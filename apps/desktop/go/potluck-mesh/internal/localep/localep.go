// Package localep discovers this node's private-LAN WireGuard endpoints so
// same-LAN peers can connect directly instead of relaying
// every packet through DERP.
//
// Background: the roster otherwise carries only each node's STUN'd public endpoint.
// Two nodes behind one NAT therefore relay through DERP even when they sit on
// the same LAN. LocalCandidates enumerates the host's own RFC1918 addresses
// and formats them as "ip:<wgPort>" so they can ride along in the roster.
//
// Privacy: only RFC1918 addresses are returned. The tailnet/CGNAT range
// (100.64.0.0/10), loopback, and link-local are all excluded via the shared
// util.PrivateLAN predicate, so a Tailscale IP is never advertised as a LAN
// candidate. The result is metadata-only (ip:port) - never payload.
//
// This package only discovers + reports candidates. It performs no routing
// and changes no DERP behaviour.
package localep

import (
	"net"
	"sort"
	"strconv"
	"strings"

	"github.com/potluck-ai/potluck-mesh/internal/util"
)

// maxCandidates caps how many LAN endpoints we advertise. Multi-homed hosts
// (e.g. a desktop with wired + WiFi + a couple of virtual bridges) can have
// several RFC1918 addresses; a handful is plenty for the direct-path trial and
// keeps the roster payload bounded. Matches the server-side cap in the
// coordinator's SignalEndpointUpdate validator.
const maxCandidates = 4

// virtualInterfacePrefixes are interface-name prefixes for container/bridge/
// virtualization adapters that hand out RFC1918-looking addresses (e.g.
// Docker's default docker0 bridge at 172.17.0.1, a per-network br-xxxxxxxx,
// libvirt's virbr0, Podman's podman0) but are never a usable path to another
// physical host on the LAN - the address is only reachable from the same
// machine's own network namespace. util.PrivateLAN only filters by IP range
// (RFC1918), which cannot tell a real LAN adapter apart from a virtual one
// sharing the same address space, so we filter by interface name too.
//
// Two concrete failure modes this prevents:
//  1. sameLANEndpoint (cmd/potluck-mesh/landirect.go) would still find the
//     genuine LAN candidate later in a peer's list even with a virtual one
//     ahead of it (it scans the whole list), but advertising an address nobody
//     can route to is pure noise on the wire and in every peer's roster.
//  2. maxCandidates caps the advertised list at 4. A host with several virtual
//     bridges (docker0 + a couple of compose per-project networks + a VPN
//     client's tun adapter) can fill the cap with unreachable addresses and
//     push the one real LAN candidate out entirely - turning "noise" into a
//     lost same-LAN direct path.
//
// Deliberately name-prefix based (not exhaustive by design): this only needs
// to catch the common container/VM engines that default to RFC1918 bridge
// subnets. A real physical adapter is never named "docker0", "br-…", etc., so
// false positives are not a practical concern on any of the three desktop
// platforms this ships on.
var virtualInterfacePrefixes = []string{
	"docker",  // Docker default bridge (docker0) + docker-proxy adapters
	"br-",     // Docker user-defined bridge networks (br-<network-id>)
	"veth",    // Docker/Linux container veth pair endpoints
	"cni",     // Kubernetes CNI bridges (cni0, etc.)
	"flannel", // Kubernetes Flannel overlay adapters
	"podman",  // Podman's default bridge (podman0/podman1)
	"virbr",   // libvirt/KVM default NAT bridge (virbr0)
	"vmnet",   // VMware host-only/NAT adapters (vmnet1/vmnet8)
	"vboxnet", // VirtualBox host-only adapters
}

// isVirtualInterfaceName reports whether ifName matches a known container/
// bridge/virtualization adapter prefix (case-insensitive - Windows adapter
// friendly names can vary in case).
func isVirtualInterfaceName(ifName string) bool {
	lower := strings.ToLower(ifName)
	for _, p := range virtualInterfacePrefixes {
		if strings.HasPrefix(lower, p) {
			return true
		}
	}
	return false
}

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
		// Skip interfaces that are down or loopback (same as lanpair), and
		// container/bridge/VM adapters that can never be a usable path to
		// another physical host (see isVirtualInterfaceName's doc).
		if ifi.Flags&net.FlagUp == 0 || ifi.Flags&net.FlagLoopback != 0 {
			continue
		}
		if isVirtualInterfaceName(ifi.Name) {
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
		if isVirtualInterfaceName(ifi.Name) {
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
			// Normalise the mask to 4 bytes when the address is IPv4 - a
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
