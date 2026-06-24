// Package localep discovers this node's private-LAN WireGuard endpoints so a
// later phase can use them for same-LAN direct connections instead of relaying
// every packet through DERP.
//
// Background: today the mesh only learns each node's STUN'd PUBLIC endpoint.
// Two nodes behind one NAT therefore relay through DERP even when they sit on
// the same LAN. LocalCandidates enumerates the host's own RFC1918 addresses
// and formats them as "ip:<wgPort>" so they can ride along in the roster.
//
// Privacy: only RFC1918 addresses are returned. The tailnet/CGNAT range
// (100.64.0.0/10), loopback, and link-local are all excluded via the shared
// util.PrivateLAN predicate, so a Tailscale IP is never advertised as a LAN
// candidate. The result is metadata-only (ip:port) — never payload.
//
// Phase 0/1 is ADDITIVE PLUMBING: this package only discovers + reports
// candidates. It performs no routing and changes no DERP behaviour.
package localep

import (
	"net"
	"sort"
	"strconv"

	"github.com/potluck-ai/potluck-mesh/internal/util"
)

// maxCandidates caps how many LAN endpoints we advertise. Multi-homed hosts
// (e.g. a desktop with wired + WiFi + a couple of virtual bridges) can have
// several RFC1918 addresses; a handful is plenty for a later phase to try and
// keeps the roster payload bounded. Matches the server-side cap in the
// coordinator's SignalEndpointUpdate validator.
const maxCandidates = 4

// LocalCandidates enumerates the host's network interfaces and returns its
// private-LAN WireGuard endpoints formatted as "ip:<wgPort>", sorted and
// deduplicated, capped at maxCandidates.
//
// Interfaces that are down or loopback are skipped (mirrors the enumeration in
// internal/lanpair). Only IPv4 *net.IPNet addresses that pass util.PrivateLAN
// are included, so loopback, link-local, and tailnet/CGNAT addresses are never
// returned.
func LocalCandidates(wgPort int) []string {
	ifaces, err := net.Interfaces()
	if err != nil {
		return nil
	}

	var ips []net.IP
	for _, ifi := range ifaces {
		// Skip interfaces that are down or loopback (same as lanpair).
		if ifi.Flags&net.FlagUp == 0 || ifi.Flags&net.FlagLoopback != 0 {
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
			ips = append(ips, ipnet.IP)
		}
	}
	return candidatesFromIPs(ips, wgPort)
}

// LocalIPNets enumerates the host's network interfaces and returns its
// private-LAN IPv4 *net.IPNet values (address + the interface's own netmask).
// The mask matters: the Phase-2 co-subnet test compares a peer's advertised LAN
// IP against MY interface mask, which is stricter than a coarse "both 192.168.x"
// heuristic and correctly rejects two hosts that share a /16 prefix but sit on
// different /24s.
//
// Same interface/address filtering as LocalCandidates: down + loopback
// interfaces are skipped, and only addresses that pass util.PrivateLAN (RFC1918,
// never tailnet/CGNAT/link-local) are returned. Each returned IPNet's IP is the
// 4-byte form and its Mask is the interface's configured mask.
func LocalIPNets() []net.IPNet {
	ifaces, err := net.Interfaces()
	if err != nil {
		return nil
	}

	var out []net.IPNet
	for _, ifi := range ifaces {
		if ifi.Flags&net.FlagUp == 0 || ifi.Flags&net.FlagLoopback != 0 {
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
			if ip4 == nil || !util.PrivateLAN(ip4) {
				continue
			}
			// Normalise the mask to 4 bytes when the address is IPv4 — a
			// dual-stack interface can report a 16-byte mask alongside a
			// 4-byte IP, which would break net.IP.Mask.
			mask := ipnet.Mask
			if len(mask) == net.IPv6len {
				mask = mask[12:]
			}
			out = append(out, net.IPNet{IP: ip4, Mask: mask})
		}
	}
	return out
}

// candidatesFromIPs applies the RFC1918 filter, "ip:port" formatting,
// dedup, sort, and cap to a raw list of interface addresses. Split out from
// LocalCandidates so the filtering logic is unit-testable without depending on
// the host's actual interface set.
func candidatesFromIPs(ips []net.IP, wgPort int) []string {
	seen := make(map[string]struct{})
	var out []string
	port := strconv.Itoa(wgPort)

	for _, ip := range ips {
		ip4 := ip.To4()
		if ip4 == nil || !util.PrivateLAN(ip4) {
			continue
		}
		ep := net.JoinHostPort(ip4.String(), port)
		if _, dup := seen[ep]; dup {
			continue
		}
		seen[ep] = struct{}{}
		out = append(out, ep)
	}

	sort.Strings(out)
	if len(out) > maxCandidates {
		out = out[:maxCandidates]
	}
	return out
}
