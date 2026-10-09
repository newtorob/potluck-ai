//go:build !meshlive_filter

package main

import (
	"github.com/potluck-ai/potluck-mesh/internal/peerstate"
	"github.com/potluck-ai/potluck-mesh/internal/tunnel"
)

// newPacketFilter builds the TUN packet filter. A household peer is an address
// the validated roster store holds right now; those pass in both directions. The
// filter has no restricted peers yet, so every other address is dropped, which
// wireguard-go would have done anyway since it only carries roster addresses.
//
// The mesh-live harness builds a variant of this function behind the
// meshlive_filter tag (scripts/dev/mesh-live/shim). The default build has no
// switch, variable or environment setting that changes what the filter does.
func newPacketFilter(ps *peerstate.Store) *tunnel.PacketFilter {
	return tunnel.NewPacketFilter(ps.IsHousehold)
}
