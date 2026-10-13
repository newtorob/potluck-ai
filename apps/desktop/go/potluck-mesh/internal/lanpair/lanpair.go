// Package lanpair helps a second machine on the local network find a
// household's pairing invite without the user copy-pasting the token by
// hand.
//
// Transport: IPv4 multicast on a link-local group (224.0.0.0/24, which routers
// must not forward off-subnet) at multicast TTL 1, plus a subnet-broadcast
// fallback for WiFi/wired topologies that drop link-local multicast (see
// broadcastSweepLoop). Only RFC1918 interfaces are used; the tailnet range
// (100.64.0.0/10) is never sent to, so pairing stays on the physical LAN.
//
// Security model: the periodic LAN announce carries only an opaque
// fingerprint of the pairing token plus the inviter's hostname - never the
// raw, redeemable token. A fingerprint is a one-way hash: it lets a listener
// notice "there's an invite from this host" and correlate it against a later
// exchange, but it cannot be replayed at the coordinator's /mesh/enroll to
// redeem household membership, and the token cannot be feasibly recovered
// from it.
//
// The actual token is handed over only through a second, PIN-gated exchange
// (RequestReveal / handleRevealRequest): the inviter's sidecar generates a
// random 6-digit PIN when it starts broadcasting and returns it to its own
// local caller only (displayed on the inviter's screen - it is never put on
// the wire). A joining machine that wants to auto-join must have a human read
// that PIN off the inviter's screen and supply it locally; only then does the
// joiner's sidecar unicast a reveal request carrying the PIN to the inviter,
// which replies with the real token if and only if the PIN matches. Reveal
// attempts are capped (maxRevealAttempts) per broadcast session, after which
// the session is burned and the broadcast is cancelled, so a LAN attacker who
// only captured the fingerprint gets a bounded number of blind guesses at a
// 6-digit space rather than an unlimited, unauthenticated redemption.
//
// This closes the "passive listener wins the race" hole a raw-token broadcast
// would have: coordinator validation of the token is the trust grant, so it
// is never sufficient to reason that broadcasting the token is safe merely
// because the coordinator also checks it - a listener who captured the raw
// token could redeem it directly against the coordinator, bypassing this
// sidecar (and the legitimate second machine) entirely.
//
// The manual/headless flow (copy-paste the token shown on the inviter's
// screen into the joiner's UI or CLI, then POST /pair) is unaffected by any
// of this: it never depended on the LAN broadcast and is already
// physical-possession-based.
package lanpair

import (
	"context"
	crand "crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/binary"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log/slog"
	"net"
	"sync"
	"time"

	"golang.org/x/net/ipv4"

	"github.com/potluck-ai/potluck-mesh/internal/util"
)

var (
	// Link-local multicast group (224.0.0.0/24 is never forwarded by routers).
	groupIP   = net.IPv4(224, 0, 0, 224)
	groupPort = 48653
)

const (
	// magic identifies the announce format. The wire format carries no raw
	// redeemable token (see package doc), so it is deliberately incompatible
	// with the POTLUCK-PAIR-1 announce (which did) rather than silently
	// interpreted with the wrong field meaning.
	magic           = "POTLUCK-PAIR-2"
	revealReqMagic  = "POTLUCK-PAIR-REVEAL-REQ-1"
	revealRespMagic = "POTLUCK-PAIR-REVEAL-RESP-1"

	sendInterval = 2 * time.Second // re-send cadence (multicast + broadcast)
	maxDatagram  = 2048

	// maxRevealAttempts bounds how many PIN guesses a broadcast session will
	// answer before it is treated as under attack and burned (broadcast
	// cancelled). A 6-digit PIN has 1e6 possibilities; capping guesses here
	// is what keeps a LAN listener who only saw the fingerprint from having
	// an unlimited number of blind tries.
	maxRevealAttempts = 5

	// revealSendInterval is the retry cadence for an outstanding reveal
	// request (in case the first unicast datagram is dropped).
	revealSendInterval = 400 * time.Millisecond
)

// announce is the periodic LAN beacon. It intentionally carries no
// redeemable secret - see the package doc.
type announce struct {
	Magic       string `json:"m"`
	Hostname    string `json:"h"`
	Fingerprint string `json:"f"`
}

// revealRequest is sent unicast, joiner -> inviter, to redeem the PIN a
// human read off the inviter's screen for the actual pairing token.
type revealRequest struct {
	Magic       string `json:"m"`
	Fingerprint string `json:"f"`
	PIN         string `json:"p"`
}

// revealResponse is sent unicast, inviter -> joiner, in answer to a
// revealRequest. Token is only populated when OK is true.
type revealResponse struct {
	Magic       string `json:"m"`
	Fingerprint string `json:"f"`
	Token       string `json:"t,omitempty"`
	OK          bool   `json:"ok"`
}

// envelope is used to peek at a datagram's message type before deciding
// which concrete struct to unmarshal into.
type envelope struct {
	Magic string `json:"m"`
}

type heard struct {
	fingerprint string
	hostname    string
	addr        *net.UDPAddr // source of the beacon; needed to unicast a reveal request
	at          time.Time
}

// bcastSession is the inviter-side state for a single active broadcast: the
// real token (never sent on the wire directly), the PIN a human must supply
// out-of-band to redeem it, and how many reveal attempts have been spent.
type bcastSession struct {
	fingerprint string
	token       string
	pin         string
	attempts    int
}

// revealOutcome is handed back to a waiting RequestReveal call once a
// matching revealResponse arrives.
type revealOutcome struct {
	token string
	ok    bool
}

// Service runs a passive listener for the lifetime of the sidecar and lets the
// IPC layer start a time-boxed broadcast on demand.
type Service struct {
	mu          sync.Mutex
	latest      *heard
	cancelBcast context.CancelFunc
	now         func() time.Time // injectable for tests

	// conn is the listener's socket, shared for sending unicast reveal
	// request/response datagrams while RunListener is active. nil otherwise.
	conn net.PacketConn

	// session is the inviter-side state for the currently active broadcast,
	// if any. nil when not broadcasting.
	session *bcastSession

	// waiters holds a delivery channel per outstanding RequestReveal call on
	// the joiner side, keyed by fingerprint.
	waiters map[string]chan revealOutcome
}

// New returns a Service. Call RunListener(ctx) once to start receiving.
func New() *Service { return &Service{now: time.Now} }

// multicastIfaces returns up, multicast-capable interfaces (including loopback
// on hosts where it is multicast-capable, so same-host send/receive works).
func multicastIfaces() []net.Interface {
	all, err := net.Interfaces()
	if err != nil {
		return nil
	}
	var out []net.Interface
	for _, ifi := range all {
		if ifi.Flags&net.FlagUp != 0 && ifi.Flags&net.FlagMulticast != 0 {
			out = append(out, ifi)
		}
	}
	return out
}

// sweepable reports whether ip is a private LAN address we may broadcast on.
// Only RFC1918 ranges qualify; loopback, link-local, and the tailnet/CGNAT
// range (100.64.0.0/10) are excluded so pairing never leaves the physical LAN.
//
// The predicate now lives in internal/util as PrivateLAN so LAN pairing and
// LAN direct-path discovery (internal/localep) share one definition. This is a
// thin alias kept for the existing call sites + tests in this package.
func sweepable(ip net.IP) bool {
	return util.PrivateLAN(ip)
}

// directedBroadcast returns the IPv4 broadcast address for ip4 under mask
// (host bits all ones), e.g. 192.168.4.86/22 -> 192.168.7.255.
func directedBroadcast(ip4 net.IP, mask net.IPMask) net.IP {
	v4 := ip4.To4()
	m4 := net.IP(mask).To4()
	if v4 == nil || m4 == nil {
		return nil
	}
	b := make(net.IP, 4)
	for i := 0; i < 4; i++ {
		b[i] = v4[i] | ^m4[i]
	}
	return b
}

// broadcastDests returns the IPv4 broadcast addresses to send the fallback to:
// the directed broadcast of each up, broadcast-capable RFC1918 interface, plus
// the limited broadcast 255.255.255.255. Sending to both covers networks that
// honor one but filter the other.
func broadcastDests() []net.IP {
	out := []net.IP{net.IPv4(255, 255, 255, 255)}
	ifaces, err := net.Interfaces()
	if err != nil {
		return out
	}
	for _, ifi := range ifaces {
		if ifi.Flags&net.FlagUp == 0 || ifi.Flags&net.FlagLoopback != 0 {
			continue
		}
		if ifi.Flags&net.FlagBroadcast == 0 {
			continue
		}
		addrs, err := ifi.Addrs()
		if err != nil {
			continue
		}
		for _, a := range addrs {
			ipnet, ok := a.(*net.IPNet)
			if !ok {
				continue
			}
			ip4 := ipnet.IP.To4()
			if ip4 == nil || !sweepable(ip4) {
				continue
			}
			// Skip /31, /32, and absurdly wide masks: a /32 (e.g. an RFC1918
			// address on a tunnel interface) has no meaningful broadcast and
			// would just self-unicast.
			if ones, bits := ipnet.Mask.Size(); bits != 32 || ones < 8 || ones > 30 {
				continue
			}
			if b := directedBroadcast(ip4, ipnet.Mask); b != nil {
				out = append(out, b)
			}
		}
	}
	return out
}

// fingerprintOf returns a short, one-way, non-redeemable correlation handle
// for token: enough entropy (48 bits) that guessing it is no easier than
// guessing the token itself, and - being a one-way hash - it cannot be
// inverted back into the token. Safe to put on an unauthenticated broadcast.
func fingerprintOf(token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:6])
}

// generatePIN returns a random 6-digit PIN using a CSPRNG. This is the
// out-of-band secret: it is shown to the human on the inviter's device and is
// never itself sent over the LAN.
func generatePIN() string {
	var b [4]byte
	if _, err := crand.Read(b[:]); err != nil {
		// crypto/rand failing is effectively unrecoverable on any real target;
		// fall back to a fixed-but-still-unbroadcast value rather than a
		// non-random one derived from time, which would be guessable.
		slog.Error("lanpair: crypto/rand unavailable for pairing PIN", "err", err)
		return "000000"
	}
	n := binary.BigEndian.Uint32(b[:]) % 1_000_000
	return fmt.Sprintf("%06d", n)
}

// RunListener joins the multicast group on every capable interface and records
// the most recent valid announce until ctx is cancelled. It also answers
// reveal requests for any broadcast session this host currently owns, and
// delivers reveal responses to any outstanding RequestReveal call.
func (s *Service) RunListener(ctx context.Context) {
	c, err := net.ListenPacket("udp4", net.JoinHostPort("0.0.0.0", itoa(groupPort)))
	if err != nil {
		slog.Warn("lanpair: listener unavailable", "err", err)
		return
	}
	defer c.Close()
	s.mu.Lock()
	s.conn = c
	s.mu.Unlock()
	defer func() {
		s.mu.Lock()
		s.conn = nil
		s.mu.Unlock()
	}()

	p := ipv4.NewPacketConn(c)
	group := &net.UDPAddr{IP: groupIP}
	joined := 0
	for _, ifi := range multicastIfaces() {
		ifi := ifi
		if err := p.JoinGroup(&ifi, group); err == nil {
			joined++
		}
	}
	if joined == 0 {
		slog.Warn("lanpair: no multicast-capable interface; LAN discovery off")
		return
	}
	_ = p.SetMulticastLoopback(true)
	slog.Info("lanpair: listening for pairing broadcasts", "group", groupIP.String(), "port", groupPort, "ifaces", joined)

	go func() {
		<-ctx.Done()
		_ = c.Close()
	}()

	buf := make([]byte, maxDatagram)
	for {
		n, _, src, err := p.ReadFrom(buf)
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			continue
		}
		var env envelope
		if json.Unmarshal(buf[:n], &env) != nil {
			continue
		}
		switch env.Magic {
		case magic:
			var a announce
			if json.Unmarshal(buf[:n], &a) != nil || a.Fingerprint == "" {
				continue
			}
			// A host with multiple multicast-capable interfaces (a VPN /
			// tailnet tunnel adapter alongside the real LAN NIC, common on a
			// dev machine) sends the same announce out every one of them
			// (see multicastSend), so more than one copy can arrive with
			// different source addresses. Only accept one whose source is
			// actually on the physical LAN (or loopback, for same-host
			// listener+broadcaster in tests): a tunnel-adapter address isn't
			// a real second machine, and - worse - a reveal request unicast
			// back to it can be silently swallowed by that tunnel's own
			// routing rather than ever reaching a listening socket, turning
			// a legitimate reveal into a silent timeout.
			addr := groupPortAddr(src)
			if addr == nil || !(addr.IP.IsLoopback() || util.PrivateLAN(addr.IP)) {
				continue
			}
			s.mu.Lock()
			s.latest = &heard{
				fingerprint: a.Fingerprint,
				hostname:    a.Hostname,
				addr:        addr,
				at:          s.now(),
			}
			s.mu.Unlock()
		case revealReqMagic:
			var rq revealRequest
			if json.Unmarshal(buf[:n], &rq) != nil || rq.Fingerprint == "" {
				continue
			}
			s.handleRevealRequest(rq, src)
		case revealRespMagic:
			var rr revealResponse
			if json.Unmarshal(buf[:n], &rr) != nil || rr.Fingerprint == "" {
				continue
			}
			s.handleRevealResponse(rr)
		}
	}
}

// groupPortAddr returns a's IP with the port forced to groupPort: the fixed
// port every lanpair listener (inviter and joiner alike) is bound to, and
// therefore the only port a reveal request/response can be answered on. a's
// own source port (an ephemeral per-broadcast send socket) is not read by
// anyone and must not be used as a reply target.
func groupPortAddr(a net.Addr) *net.UDPAddr {
	u, ok := a.(*net.UDPAddr)
	if !ok || u == nil {
		return nil
	}
	return &net.UDPAddr{IP: u.IP, Port: groupPort}
}

// Discovered returns the freshest heard invite within maxAge, if any. It
// returns a fingerprint - not the redeemable token, which is never
// broadcast - so a caller can show "found an invite from HOST" and then use
// RequestReveal (after a human supplies the inviter's PIN) to actually obtain
// the token.
func (s *Service) Discovered(maxAge time.Duration) (fingerprint, hostname string, ok bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.latest == nil || s.now().Sub(s.latest.at) > maxAge {
		return "", "", false
	}
	return s.latest.fingerprint, s.latest.hostname, true
}

// RequestReveal asks the LAN host that broadcast fingerprint to reveal the
// real pairing token, authenticating with pin - a value a human must read off
// the inviter's screen and supply locally; it is never transmitted by the
// periodic LAN announce. A passive listener that only captured the broadcast
// fingerprint cannot complete this exchange without also guessing the PIN,
// and guesses are capped by the inviter (see maxRevealAttempts).
//
// Returns an error if no recent beacon matches fingerprint, the PIN is
// rejected, or the inviter doesn't answer within timeout.
func (s *Service) RequestReveal(fingerprint, pin string, timeout time.Duration) (string, error) {
	target := s.targetFor(fingerprint)
	if target == nil {
		return "", fmt.Errorf("no recent invite matches this fingerprint")
	}

	s.mu.Lock()
	conn := s.conn
	if s.waiters == nil {
		s.waiters = make(map[string]chan revealOutcome)
	}
	ch := make(chan revealOutcome, 1)
	s.waiters[fingerprint] = ch
	s.mu.Unlock()

	defer func() {
		s.mu.Lock()
		delete(s.waiters, fingerprint)
		s.mu.Unlock()
	}()

	if conn == nil {
		return "", fmt.Errorf("LAN pairing listener is not running")
	}

	payload, err := json.Marshal(revealRequest{Magic: revealReqMagic, Fingerprint: fingerprint, PIN: pin})
	if err != nil {
		return "", err
	}

	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()

	if _, err := conn.WriteTo(payload, target); err != nil {
		return "", fmt.Errorf("could not reach inviter on LAN: %w", err)
	}
	t := time.NewTicker(revealSendInterval)
	defer t.Stop()
	for {
		select {
		case out := <-ch:
			if !out.ok {
				return "", fmt.Errorf("incorrect pairing PIN")
			}
			return out.token, nil
		case <-t.C:
			// Re-resolve the target on every retry rather than reusing the
			// address captured at the start: on a multi-homed host the
			// inviter's periodic re-announce (every sendInterval, on every
			// multicast-capable interface) may update the freshest known
			// source address between ticks, and the first one recorded is
			// not guaranteed to be the one that actually answers back.
			if fresh := s.targetFor(fingerprint); fresh != nil {
				target = fresh
			}
			_, _ = conn.WriteTo(payload, target)
		case <-ctx.Done():
			return "", fmt.Errorf("no response from inviter (timed out)")
		}
	}
}

// targetFor returns the most recently recorded source address for
// fingerprint, if any.
func (s *Service) targetFor(fingerprint string) *net.UDPAddr {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.latest != nil && s.latest.fingerprint == fingerprint {
		return s.latest.addr
	}
	return nil
}

// handleRevealRequest answers a unicast reveal request if and only if this
// host currently owns a broadcast session matching rq.Fingerprint and the
// supplied PIN is correct, subject to a per-session attempt cap. Once the cap
// is hit without a correct PIN, the session is treated as under attack: no
// further requests are answered and the broadcast itself is cancelled.
func (s *Service) handleRevealRequest(rq revealRequest, src net.Addr) {
	s.mu.Lock()
	sess := s.session
	if sess == nil || sess.fingerprint != rq.Fingerprint {
		s.mu.Unlock()
		return // not (or no longer) our session - say nothing
	}
	if sess.attempts >= maxRevealAttempts {
		s.mu.Unlock()
		return // already burned; ignore silently
	}
	sess.attempts++
	ok := subtle.ConstantTimeCompare([]byte(sess.pin), []byte(rq.PIN)) == 1
	var token string
	if ok {
		token = sess.token
	}
	exhausted := !ok && sess.attempts >= maxRevealAttempts
	s.mu.Unlock()

	resp := revealResponse{Magic: revealRespMagic, Fingerprint: rq.Fingerprint, OK: ok, Token: token}
	payload, err := json.Marshal(resp)
	if err == nil {
		_ = s.sendTo(payload, src)
	}

	if ok {
		slog.Info("lanpair: pairing PIN confirmed; token revealed to LAN peer")
		return
	}
	if exhausted {
		slog.Warn("lanpair: pairing PIN attempts exhausted; cancelling broadcast",
			"fingerprint", rq.Fingerprint)
		s.StopBroadcast()
	}
}

func (s *Service) handleRevealResponse(rr revealResponse) {
	s.mu.Lock()
	ch, ok := s.waiters[rr.Fingerprint]
	if ok {
		delete(s.waiters, rr.Fingerprint)
	}
	s.mu.Unlock()
	if !ok {
		return
	}
	select {
	case ch <- revealOutcome{token: rr.Token, ok: rr.OK}:
	default:
	}
}

func (s *Service) sendTo(payload []byte, dst net.Addr) error {
	s.mu.Lock()
	c := s.conn
	s.mu.Unlock()
	if c == nil {
		return fmt.Errorf("lanpair: no active listener socket")
	}
	_, err := c.WriteTo(payload, dst)
	return err
}

// StartBroadcast (re)starts a time-boxed broadcast advertising a pairing
// fingerprint (never the raw token) on the LAN for window, and returns a
// freshly generated confirmation PIN for the caller to display locally (e.g.
// on the inviter's screen). A joining machine can only obtain the actual
// token via RequestReveal once a human has supplied that PIN. Any prior
// broadcast is cancelled first, so at most one is ever active. There is
// deliberately no IPC "stop early" path beyond StopBroadcast: the window is
// short, the token is single-use, and the group is link-local, so letting it
// self-expire keeps the surface minimal.
func (s *Service) StartBroadcast(token, hostname string, window time.Duration) string {
	fp := fingerprintOf(token)
	pin := generatePIN()

	s.mu.Lock()
	if s.cancelBcast != nil {
		s.cancelBcast()
	}
	ctx, cancel := context.WithTimeout(context.Background(), window)
	s.cancelBcast = cancel
	s.session = &bcastSession{fingerprint: fp, token: token, pin: pin}
	s.mu.Unlock()

	go s.broadcastLoop(ctx, fp, hostname)
	return pin
}

// StopBroadcast cancels any active broadcast and clears its session, so no
// further reveal requests are answered for it.
func (s *Service) StopBroadcast() {
	s.mu.Lock()
	if s.cancelBcast != nil {
		s.cancelBcast()
		s.cancelBcast = nil
	}
	s.session = nil
	s.mu.Unlock()
}

// clearSessionIfMatches drops the active session once its broadcast loop
// exits (window expired, or the socket failed), so a reveal request arriving
// after expiry gets no answer even if s.cancelBcast was since overwritten by
// a newer StartBroadcast call.
func (s *Service) clearSessionIfMatches(fingerprint string) {
	s.mu.Lock()
	if s.session != nil && s.session.fingerprint == fingerprint {
		s.session = nil
	}
	s.mu.Unlock()
}

func (s *Service) broadcastLoop(ctx context.Context, fingerprint, hostname string) {
	defer s.clearSessionIfMatches(fingerprint)

	c, err := net.ListenPacket("udp4", "0.0.0.0:0")
	if err != nil {
		slog.Warn("lanpair: broadcast socket failed", "err", err)
		return
	}
	defer c.Close()
	p := ipv4.NewPacketConn(c)
	_ = p.SetMulticastTTL(1) // stay on the subnet
	_ = p.SetMulticastLoopback(true)
	dst := &net.UDPAddr{IP: groupIP, Port: groupPort}
	payload, err := json.Marshal(announce{Magic: magic, Hostname: hostname, Fingerprint: fingerprint})
	if err != nil {
		return
	}

	// Multicast is the fast path on every capable interface.
	multicastSend := func() {
		for _, ifi := range multicastIfaces() {
			ifi := ifi
			if err := p.SetMulticastInterface(&ifi); err != nil {
				continue
			}
			_, _ = p.WriteTo(payload, nil, dst)
		}
	}

	// The broadcast fallback runs on its own goroutine and its own socket (it
	// needs SO_BROADCAST, which we never want on the multicast socket).
	go s.broadcastSweepLoop(ctx, payload)

	multicastSend() // send one immediately
	t := time.NewTicker(sendInterval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			multicastSend()
		}
	}
}

// broadcastSweepLoop sends the payload to the subnet broadcast address(es) as a
// fallback for networks that drop link-local multicast (common over WiFi). A
// single datagram per destination reaches the whole segment, with no per-host
// ARP. The receiver's listener is bound to 0.0.0.0:groupPort, so it accepts
// these with no extra group join.
func (s *Service) broadcastSweepLoop(ctx context.Context, payload []byte) {
	c, err := net.ListenPacket("udp4", "0.0.0.0:0")
	if err != nil {
		slog.Warn("lanpair: broadcast socket failed", "err", err)
		return
	}
	defer c.Close()
	// SO_BROADCAST is required to send to broadcast addresses. If we cannot set
	// it, the broadcast fallback is off (multicast still runs); say so and stop
	// rather than silently failing every WriteTo with EACCES.
	uc, ok := c.(*net.UDPConn)
	if !ok {
		slog.Warn("lanpair: broadcast socket lacks raw conn; fallback off")
		return
	}
	sc, e := uc.SyscallConn()
	if e != nil {
		slog.Warn("lanpair: broadcast raw conn unavailable; fallback off", "err", e)
		return
	}
	var setErr error
	_ = sc.Control(func(fd uintptr) { setErr = enableBroadcast(fd) })
	if setErr != nil {
		slog.Warn("lanpair: enabling SO_BROADCAST failed; fallback off", "err", setErr)
		return
	}

	// Recompute destinations each tick: interfaces can come up or change address
	// mid-window (WiFi associating, DHCP), and the first send may be too early.
	send := func() {
		for _, d := range broadcastDests() {
			_, _ = c.WriteTo(payload, &net.UDPAddr{IP: d, Port: groupPort})
		}
	}
	send() // send one immediately
	t := time.NewTicker(sendInterval)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			send()
		}
	}
}

// itoa avoids importing strconv for a single small conversion.
func itoa(n int) string {
	if n == 0 {
		return "0"
	}
	var b [20]byte
	i := len(b)
	for n > 0 {
		i--
		b[i] = byte('0' + n%10)
		n /= 10
	}
	return string(b[i:])
}
