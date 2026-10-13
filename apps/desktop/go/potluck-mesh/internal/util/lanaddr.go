package util

import "net"

// PrivateLAN reports whether ip is a private LAN address that may be used as a
// direct-path / pairing candidate. Only RFC1918 ranges qualify
// (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16). Loopback, IPv4 link-local
// (169.254/16), and the tailnet/CGNAT range (100.64.0.0/10) are excluded so a
// Tailscale address is never mistaken for a physical-LAN endpoint.
//
// This is the single source of truth for the RFC1918 predicate, so LAN-pairing
// broadcast and LAN direct-path discovery share exactly the same filter.
func PrivateLAN(ip net.IP) bool {
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
