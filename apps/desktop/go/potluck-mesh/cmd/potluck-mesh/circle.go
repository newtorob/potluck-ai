package main

import (
	"net"
	"net/netip"

	"github.com/potluck-ai/potluck-mesh/internal/circlelink"
	"github.com/potluck-ai/potluck-mesh/internal/derp"
	"github.com/potluck-ai/potluck-mesh/internal/localapi"
	"github.com/potluck-ai/potluck-mesh/internal/peerstate"
	msignal "github.com/potluck-ai/potluck-mesh/internal/signal"
	"github.com/potluck-ai/potluck-mesh/internal/tunnel"
)

// circleTunnel is the WireGuard device as the circle controller uses it, plus the
// hook that tells SetPeers which peers to keep. *tunnel.Tunnel satisfies it.
type circleTunnel interface {
	circlelink.Tunnel
	SetKeptPeers(func() []tunnel.Peer)
}

// circleDeps is what the circle controller is wired to.
type circleDeps struct {
	tun    circleTunnel
	filter *tunnel.PacketFilter
	dBind  *derp.DerpBind // nil when no relay is configured
	ps     *peerstate.Store
	client *msignal.Client
	ipc    *localapi.State

	// symmetricNAT reports this node's NAT was classified as symmetric.
	symmetricNAT func() bool
	// localNets lists this host's private LAN interfaces (localep.LocalIPNets).
	localNets func() []net.IPNet
}

// circleGuard is what the roster handler tells the circle controller: these
// roster entries are household machines now.
type circleGuard interface {
	HouseholdChanged(self string, entries []msignal.RosterPeer)
}

// setupCircleLinks builds the controller and connects it to the tunnel (so a
// welcome keeps its peers), to the signal client (messages in, acks out, the
// feature in hello) and to the local API (the feature in /status).
//
// It does nothing to the household path: circle peers are not in the household
// store, never reach the reachability, LAN-direct or punch-trial loops, and are
// published nowhere on the event bus.
func setupCircleLinks(d circleDeps) *circlelink.Controller {
	cfg := circlelink.Config{
		Tunnel:    d.tun,
		Filter:    d.filter,
		Signal:    d.client,
		Household: d.ps,
		Self: func() (netip.Addr, string) {
			a, _ := netip.ParseAddr(d.ipc.MeshIP())
			return a, d.ipc.WGPublicKey()
		},
		SkipDirect: d.symmetricNAT,
		PickLAN: func(candidates []string) (string, bool) {
			if d.localNets == nil {
				return "", false
			}
			return sameLANEndpoint(d.localNets(), candidates)
		},
	}
	// A nil *DerpBind inside the interface would not be a nil interface.
	if d.dBind != nil {
		cfg.Relay = d.dBind
	}
	c := circlelink.New(cfg)
	d.tun.SetKeptPeers(c.Kept)
	d.client.SetCircleHandler(c)
	d.client.SetFeatures(msignal.SidecarFeatures())
	d.ipc.SetFeatures(msignal.SidecarFeatures())
	return c
}

// setCircle wires the controller the roster handler consults. nil-safe.
func (h *rosterHandler) setCircle(g circleGuard) {
	h.circle = g
}

// guardCircle tells the circle controller about household entries the store has
// just accepted, before the tunnel is configured for them. See
// circlelink.Controller.HouseholdChanged for why the circle peer is the one that
// goes.
func (h *rosterHandler) guardCircle(self string, entries []msignal.RosterPeer) {
	if h.circle == nil {
		return
	}
	h.circle.HouseholdChanged(self, entries)
}
