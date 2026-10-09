//go:build meshlive_filter

// Harness-only, used by the WITH_LINKS hostile-requester phase (LH) and by nothing
// else. Dockerfile.sidecar copies this file into cmd/potluck-mesh/ of a scratch
// copy of the source (SHIM=hostile), where it replaces the shipped
// packetfilter.go through the build tag meshlive_filter, and in the same scratch
// copy widens the circle peer's allowed-ips to the lender's household range. The
// result is a requester that has been rewritten to try to leave its grant: its
// own device sends anything to anywhere the circle peer's allowed-ips cover, and
// its own packet filter treats every address as a household peer. The only thing
// left to stop it is the lender's side, which is what the phase measures.
// Nothing here exists in the default build.
package main

import (
	"log/slog"

	"github.com/potluck-ai/potluck-mesh/internal/peerstate"
	"github.com/potluck-ai/potluck-mesh/internal/tunnel"
)

func newPacketFilter(ps *peerstate.Store) *tunnel.PacketFilter {
	slog.Warn("MESHLIVE OPEN FILTER SHIM ACTIVE: harness build of a hostile requester, every address counts as a household peer")
	return tunnel.NewPacketFilter(func([4]byte) bool { return true })
}
