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
	sendInterval = 2 * time.Second // re-send cadence during the broadcast window
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

	send := func() {
		for _, ifi := range multicastIfaces() {
			ifi := ifi
			if err := p.SetMulticastInterface(&ifi); err != nil {
				continue
			}
			_, _ = p.WriteTo(payload, nil, dst)
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
