package peerstate

import (
	"encoding/base64"
	"encoding/binary"
	"errors"
	"fmt"
	"log/slog"
	"net/netip"
	"os"
	"strings"

	"github.com/potluck-ai/potluck-mesh/internal/signal"
	"github.com/potluck-ai/potluck-mesh/internal/util"
)

// defaultMeshCIDR is the pool the coordinator carves one /24 per household out
// of (services/coordinator/coordinator/services/household_allocator.py,
// HOUSEHOLD_POOL).
const defaultMeshCIDR = "100.64.0.0/10"

// meshRange returns the range every roster mesh_ip must fall in: the
// POTLUCK_MESH_CIDR override when it parses, otherwise the default pool. This is
// the same rule the engine applies (_resolve_mesh_network in
// apps/desktop/python/potluck_local/app.py), limited to CIDR notation.
//
// The welcome message carries this node's mesh_ip but not the household's
// mesh_cidr (only the enroll response has it, and it is not persisted), so the
// household /24 is not reliably known when a roster is applied. The pool is
// also the only range that stays right when circle members from other
// households share one roster.
func meshRange() netip.Prefix {
	if raw := strings.TrimSpace(os.Getenv("POTLUCK_MESH_CIDR")); raw != "" {
		if p, err := netip.ParsePrefix(raw); err == nil {
			return p.Masked()
		}
		slog.Warn("peerstate: POTLUCK_MESH_CIDR is not a valid CIDR, using the default mesh range",
			"default", defaultMeshCIDR,
		)
	}
	return netip.MustParsePrefix(defaultMeshCIDR)
}

// lastAddr returns the highest address of an IPv4 range (its broadcast address).
func lastAddr(p netip.Prefix) netip.Addr {
	a := p.Masked().Addr().As4()
	host := uint(32 - p.Bits())
	v := uint64(binary.BigEndian.Uint32(a[:])) | (uint64(1)<<host - 1)
	var out [4]byte
	binary.BigEndian.PutUint32(out[:], uint32(v))
	return netip.AddrFrom4(out)
}

// validateMeshIP checks one roster mesh_ip and returns it parsed. The input is
// untrusted wire data and ends up in a WireGuard allowed-ip, a kernel host
// route and GET /peers, so it must be a bare dotted IPv4 address (no prefix,
// zone, port, whitespace or control characters: netip.ParseAddr is strict about
// all of those), inside mesh, a usable host address, and not this node's own.
//
// The returned error text never echoes the raw input, only the parsed address,
// so a hostile value cannot reach the logs.
func validateMeshIP(raw string, mesh netip.Prefix, self netip.Addr) (netip.Addr, error) {
	ip, err := netip.ParseAddr(raw)
	if err != nil || !ip.Is4() {
		return netip.Addr{}, errors.New("mesh_ip is not a plain IPv4 address")
	}
	switch {
	case ip.IsUnspecified():
		return netip.Addr{}, fmt.Errorf("mesh_ip %s is the unspecified address", ip)
	case ip.IsLoopback():
		return netip.Addr{}, fmt.Errorf("mesh_ip %s is a loopback address", ip)
	case ip.IsMulticast():
		return netip.Addr{}, fmt.Errorf("mesh_ip %s is a multicast address", ip)
	case ip.IsLinkLocalUnicast():
		return netip.Addr{}, fmt.Errorf("mesh_ip %s is a link-local address", ip)
	case !mesh.Contains(ip):
		return netip.Addr{}, fmt.Errorf("mesh_ip %s is outside the mesh range %s", ip, mesh)
	case ip == lastAddr(mesh):
		return netip.Addr{}, fmt.Errorf("mesh_ip %s is the broadcast address of %s", ip, mesh)
	case ip == self:
		return netip.Addr{}, fmt.Errorf("mesh_ip %s is this node's own address", ip)
	}
	return ip, nil
}

// validateWGKey checks a roster wg_public_key: standard base64 of exactly 32
// bytes, in the canonical form util.WGDerivePublicKey produces. The tunnel
// layer only checks that the key is decodable base64 (base64ToHex), and
// wireguard-go enforces the length later inside IpcSet, where one wrong-length
// key aborts the rest of a SetPeers batch after replace_peers has already
// cleared the device. The decoder also skips embedded CR/LF, so the re-encode
// comparison is what rejects a key that only decodes because of those.
func validateWGKey(raw string) error {
	b, err := base64.StdEncoding.DecodeString(raw)
	if err != nil || len(b) != util.WGKeyLen || base64.StdEncoding.EncodeToString(b) != raw {
		return errors.New("wg_public_key is not a canonical base64 32-byte key")
	}
	return nil
}

// checkEntry applies the rules that look at one entry on its own.
func checkEntry(p signal.RosterPeer, mesh netip.Prefix, self netip.Addr) (netip.Addr, error) {
	ip, err := validateMeshIP(p.MeshIP, mesh, self)
	if err != nil {
		return netip.Addr{}, err
	}
	if err := validateWGKey(p.WGPublicKey); err != nil {
		return netip.Addr{}, err
	}
	return ip, nil
}

// judge decides which incoming entries may be stored. It returns one reason per
// incoming entry (empty means admitted) and the node_ids of held entries whose
// address ended up claimed by more than one node.
//
// held are entries the store already has. They keep their address for the
// whole call, so an address a peer is giving up in this same call becomes
// usable from the next one. Claimants are counted by node_id, so a node
// appearing in both held and incoming with one address is a single claimant.
//
// An address with more than one claimant is given to none of them. When two
// keys claim one allowed-ip, wireguard-go does not refuse the second: the
// insert replaces the owner, so whichever peer was configured last silently
// takes the address (and the traffic) from the other, and which one that is
// depends on roster order. The roster cannot say which claimant is the real
// owner, and sending a household's requests to the wrong machine is worse than
// not reaching either, so neither is admitted.
func judge(incoming, held []signal.RosterPeer, mesh netip.Prefix, self netip.Addr) (reasons, lostHeld []string) {
	reasons = make([]string, len(incoming))
	addrs := make([]netip.Addr, len(incoming))
	owner := make(map[netip.Addr]string)
	contested := make(map[netip.Addr]bool)
	claim := func(a netip.Addr, nodeID string) {
		if first, ok := owner[a]; !ok {
			owner[a] = nodeID
		} else if first != nodeID {
			contested[a] = true
		}
	}

	for _, h := range held {
		// Held entries passed checkEntry when they were stored.
		if a, err := netip.ParseAddr(h.MeshIP); err == nil {
			claim(a, h.NodeID)
		}
	}
	for i, p := range incoming {
		a, err := checkEntry(p, mesh, self)
		if err != nil {
			reasons[i] = err.Error()
			continue
		}
		addrs[i] = a
		claim(a, p.NodeID)
	}
	for i := range incoming {
		if reasons[i] == "" && contested[addrs[i]] {
			reasons[i] = fmt.Sprintf("mesh_ip %s is claimed by more than one node", addrs[i])
		}
	}
	for _, h := range held {
		if a, err := netip.ParseAddr(h.MeshIP); err == nil && contested[a] {
			lostHeld = append(lostHeld, h.NodeID)
		}
	}
	return reasons, lostHeld
}

// Rejection records one roster entry the store refused. Reason is a short fixed
// phrase that never contains the wire value or any part of a key.
type Rejection struct {
	NodeID string
	Reason string
}

// warnRejected writes the one log line per dropped entry.
func warnRejected(rejected []Rejection) {
	for _, r := range rejected {
		slog.Warn("roster: dropping peer", "node_id", r.NodeID, "reason", r.Reason)
	}
}

// CheckMeshAddr applies the roster's address rules to raw: a bare dotted IPv4
// host address inside this store's mesh range, not the broadcast address and not
// self. Callers that hold peers outside the roster (circle links) use it so one
// definition of "a usable mesh address" covers both. The returned error never
// echoes raw.
func (s *Store) CheckMeshAddr(raw string, self netip.Addr) (netip.Addr, error) {
	return validateMeshIP(raw, s.mesh, self)
}

// ValidateWGKey applies the roster's key rule to raw: canonical base64 of 32
// bytes.
func ValidateWGKey(raw string) error { return validateWGKey(raw) }
