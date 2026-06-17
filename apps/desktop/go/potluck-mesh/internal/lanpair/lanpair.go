// Package lanpair broadcasts a pairing token over the local network so a
// second machine on the same LAN can join a household without the user
// copy-pasting the token by hand.
//
// Transport: IPv4 multicast on a link-local group (224.0.0.0/24, which routers
// must not forward off-subnet) at multicast TTL 1. Each datagram carries ONLY
// the opaque pairing token plus the inviter's hostname. No household id, node
// id, member id, or any other identity is broadcast. The coordinator still
// validates the token on enroll, so the LAN broadcast only removes the manual
// copy-paste; it grants no trust by itself. A machine that is merely listening
// transmits nothing, so listening reveals no presence.
//
// Broadcast fallback: many consumer WiFi/mesh routers silently drop link-local
// multicast between a wireless client and a wired one, so multicast discovery
// finds nothing on a very common home topology (verified on the bench network:
// a WiFi laptop and a wired desktop on one /22 never saw each other's multicast,
// while an L2 broadcast crossed fine). So during the broadcast window the inviter
// ALSO sends the same datagram to the IPv4 subnet broadcast address (directed
// per interface, plus the limited 255.255.255.255). One datagram reaches every
// host on the segment, with no per-host ARP and no dependence on multicast
// forwarding. This changes only the delivery: same payload, same opaque
// single-use token, same coordinator validation, and the joining machine still
// only listens (it transmits nothing, so it still reveals no presence). Only
// RFC1918 interfaces are used; the tailnet range (100.64.0.0/10) is never sent
// to, so pairing stays on the physical LAN.
package lanpair

import (
	"context"
	"encoding/json"
	"log/slog"
	"net"
	"sync"
	"time"

	"golang.org/x/net/ipv4"
)

var (
	// Link-local multicast group (224.0.0.0/24 is never forwarded by routers).
	groupIP   = net.IPv4(224, 0, 0, 224)
	groupPort = 48653
)

const (
	magic        = "POTLUCK-PAIR-1"
	sendInterval = 2 * time.Second // re-send cadence (multicast + broadcast)
	maxDatagram  = 2048
)

type announce struct {
	Magic    string `json:"m"`
	Token    string `json:"t"`
	Hostname string `json:"h"`
}

type heard struct {
	token    string
	hostname string
	at       time.Time
}

// Service runs a passive listener for the lifetime of the sidecar and lets the
// IPC layer start a time-boxed broadcast on demand.
type Service struct {
	mu          sync.Mutex
	latest      *heard
	cancelBcast context.CancelFunc
	now         func() time.Time // injectable for tests
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
func sweepable(ip net.IP) bool {
	ip4 := ip.To4()
	if ip4 == nil || ip4.IsLoopback() || ip4.IsLinkLocalUnicast() {
		return false
	}
	if ip4[0] == 100 && ip4[1] >= 64 && ip4[1] <= 127 { // 100.64.0.0/10 (Tailscale/CGNAT)
		return false
	}
	switch {
	case ip4[0] == 10:
		return true
	case ip4[0] == 172 && ip4[1] >= 16 && ip4[1] <= 31:
		return true
	case ip4[0] == 192 && ip4[1] == 168:
		return true
	}
	return false
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

// RunListener joins the multicast group on every capable interface and records
// the most recent valid announce until ctx is cancelled. Receive-only.
func (s *Service) RunListener(ctx context.Context) {
	c, err := net.ListenPacket("udp4", net.JoinHostPort("0.0.0.0", itoa(groupPort)))
	if err != nil {
		slog.Warn("lanpair: listener unavailable", "err", err)
		return
	}
	defer c.Close()
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
		n, _, _, err := p.ReadFrom(buf) // source addr discarded on purpose
		if err != nil {
			if ctx.Err() != nil {
				return
			}
			continue
		}
		var a announce
		if json.Unmarshal(buf[:n], &a) != nil || a.Magic != magic || a.Token == "" {
			continue
		}
		s.mu.Lock()
		s.latest = &heard{token: a.Token, hostname: a.Hostname, at: s.now()}
		s.mu.Unlock()
	}
}

// Discovered returns the freshest heard token within maxAge, if any.
func (s *Service) Discovered(maxAge time.Duration) (token, hostname string, ok bool) {
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.latest == nil || s.now().Sub(s.latest.at) > maxAge {
		return "", "", false
	}
	return s.latest.token, s.latest.hostname, true
}

// StartBroadcast (re)starts a time-boxed broadcast of token+hostname. Any prior
// broadcast is cancelled first, so at most one is ever active. There is
// deliberately no IPC "stop early" path: the window is short, the token is
// single-use, and the group is link-local, so letting it self-expire keeps the
// surface minimal. StopBroadcast exists for tests / shutdown only.
func (s *Service) StartBroadcast(token, hostname string, window time.Duration) {
	s.mu.Lock()
	if s.cancelBcast != nil {
		s.cancelBcast()
	}
	ctx, cancel := context.WithTimeout(context.Background(), window)
	s.cancelBcast = cancel
	s.mu.Unlock()
	go s.broadcastLoop(ctx, token, hostname)
}

// StopBroadcast cancels any active broadcast.
func (s *Service) StopBroadcast() {
	s.mu.Lock()
	if s.cancelBcast != nil {
		s.cancelBcast()
		s.cancelBcast = nil
	}
	s.mu.Unlock()
}

func (s *Service) broadcastLoop(ctx context.Context, token, hostname string) {
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
	payload, err := json.Marshal(announce{Magic: magic, Token: token, Hostname: hostname})
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
