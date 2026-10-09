package main

import (
	"encoding/base64"
	"net"
	"sync"
	"testing"
	"time"

	"github.com/potluck-ai/potluck-mesh/internal/derp"
	"github.com/potluck-ai/potluck-mesh/internal/peerstate"
	msignal "github.com/potluck-ai/potluck-mesh/internal/signal"
	"github.com/potluck-ai/potluck-mesh/internal/tunnel"
)

type recoveryTunnel struct {
	*tunnel.Tunnel // Unexpected OS/tunnel operations fail the test.
	handshake      time.Time
	mu             sync.Mutex // guards added: the second machine of a pair sets its keepalive from a timer
	added          []tunnel.Peer
	removed        int
}

func (f *recoveryTunnel) PeerReachability(string) (time.Time, uint64, uint64, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	return f.handshake, 0, 0, nil
}
func (f *recoveryTunnel) AddPeer(p tunnel.Peer) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.added = append(f.added, p)
	return nil
}
func (f *recoveryTunnel) RemovePeer(string) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.removed++
	return nil
}
func (f *recoveryTunnel) AddPeerRoute(string) error               { return nil }
func (f *recoveryTunnel) UpdatePeerEndpoint(string, string) error { return nil }

type recoverySignal struct {
	mu       sync.Mutex
	requests []string
}

func (f *recoverySignal) SendEndpointUpdate(string, []string) {}
func (f *recoverySignal) SendRequestPunch(id string) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.requests = append(f.requests, id)
}

func recoveryFixture(t *testing.T) (*rosterHandler, *recoveryTunnel, *recoverySignal, msignal.RosterPeer, [32]byte) {
	t.Helper()
	pub := [32]byte{1}
	wan := "203.0.113.2:51820"
	p := msignal.RosterPeer{NodeID: "peer", WGPublicKey: base64.StdEncoding.EncodeToString(pub[:]), MeshIP: "100.64.0.2", LastEndpoint: &wan, LocalEndpoints: []string{"192.168.1.20:51820"}}
	tun := &recoveryTunnel{handshake: time.Now()}
	h := newRosterHandler(tun, peerstate.New(), nil, nil)
	h.ps.Upsert(p)
	h.localIPNets = func() []net.IPNet { return []net.IPNet{mustIPNet(t, "192.168.1.10/24")} }
	h.modes = newModeTracker()
	h.punchTrials = newPunchTrialTracker()
	h.dBind = derp.NewDerpBind(nil, nil)
	h.dBind.SetPeerRouting(pub, true)
	signal := &recoverySignal{}
	h.client = signal
	return h, tun, signal, p, pub
}

// Reproduce the incident: direct failed, relay handshakes remain fresh, and
// endpoints stay unchanged. Recovery must happen after backoff without a
// restart/roster event, while repeated or concurrent ticks stay throttled.
func TestRelayedLANRecoveryAfterBackoff(t *testing.T) {
	h, _, signal, p, _ := recoveryFixture(t)
	h.punchTrials.setBackoff(p.WGPublicKey, time.Now().Add(time.Minute))
	h.retryRelayedLANPeers()
	if len(signal.requests) != 0 {
		t.Fatal("retry during backoff")
	}
	h.punchTrials.setBackoff(p.WGPublicKey, time.Now().Add(-time.Second))
	var wg sync.WaitGroup
	for i := 0; i < 10; i++ {
		wg.Add(1)
		go func() { defer wg.Done(); h.retryRelayedLANPeers() }()
	}
	wg.Wait()
	if len(signal.requests) != 1 || signal.requests[0] != p.NodeID {
		t.Fatalf("requests = %v, want one coordinated retry", signal.requests)
	}
	if !h.peerRelayed(p.WGPublicKey) {
		t.Fatal("request itself must not remove relay fallback")
	}
}

func TestRelayedLANRecoverySkipsIneligiblePeers(t *testing.T) {
	for _, name := range []string{"fresh direct", "LAN trial", "LAN direct", "punch trial", "punch direct", "offline", "no handshake", "WAN only", "no endpoint", "no relay"} {
		t.Run(name, func(t *testing.T) {
			h, tun, signal, p, pub := recoveryFixture(t)
			switch name {
			case "fresh direct":
				h.dBind.SetPeerRouting(pub, false)
			case "LAN trial":
				h.modes.set(p.WGPublicKey, modeLANTrial)
			case "LAN direct":
				h.modes.set(p.WGPublicKey, modeLANDirect)
			case "punch trial":
				h.modes.set(p.WGPublicKey, modePunchTrial)
			case "punch direct":
				h.modes.set(p.WGPublicKey, modeDirect)
			case "offline":
				tun.handshake = time.Now().Add(-handshakeFreshWindow - time.Second)
			case "no handshake":
				tun.handshake = time.Time{}
			case "WAN only":
				p.LocalEndpoints = []string{"192.168.2.20:51820"}
				h.ps.Upsert(p)
			case "no endpoint":
				p.LastEndpoint = nil
				h.ps.Upsert(p)
			case "no relay":
				h.dBind = nil
			}
			h.retryRelayedLANPeers()
			if len(signal.requests) != 0 {
				t.Fatalf("unexpected requests %v", signal.requests)
			}
		})
	}
}

func TestPunchCoordSameLANBypassesOnlyNATVeto(t *testing.T) {
	for _, name := range []string{"same LAN", "WAN only", "backoff", "managed"} {
		t.Run(name, func(t *testing.T) {
			h, tun, signal, p, _ := recoveryFixture(t)
			h.isSymmetricNATFn = func() bool { return true }
			switch name {
			case "WAN only":
				p.LocalEndpoints = nil
				h.ps.Upsert(p)
			case "backoff":
				h.punchTrials.setBackoff(p.WGPublicKey, time.Now().Add(time.Minute))
			case "managed":
				h.modes.set(p.WGPublicKey, modeDirect)
			}
			h.maybeSendRequestPunch(p.NodeID)
			h.OnPunchCoord(msignal.PunchCoord{PeerNodeID: p.NodeID, PeerWGPublicKey: p.WGPublicKey, PeerEndpoint: p.LastEndpoint, Role: "initiator"})
			if name != "same LAN" {
				if len(signal.requests) != 0 || len(tun.added) != 0 {
					t.Fatal("NAT exception bypassed a required guard")
				}
				return
			}
			if len(signal.requests) != 1 || len(tun.added) != 1 {
				t.Fatal("same-LAN trial incorrectly blocked by WAN NAT")
			}
			if tun.added[0].Endpoint != "192.168.1.20:51820" {
				t.Fatalf("wrong endpoint %q", tun.added[0].Endpoint)
			}
			if h.modes.get(p.WGPublicKey) != modePunchTrial {
				t.Fatal("must verify before declaring direct")
			}
			st, ok := h.punchTrials.get(p.WGPublicKey)
			if !ok || st.deadline.IsZero() {
				t.Fatal("missing timeout/fallback bookkeeping")
			}
		})
	}
}
