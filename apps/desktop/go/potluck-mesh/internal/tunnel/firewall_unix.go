//go:build darwin

package tunnel

// allowMeshInbound / removeMeshInbound are no-ops on macOS: macOS's packet
// filter (pf) is disabled by default (`pfctl -s all` reports
// "Status: Disabled" on a stock install) and the mesh code does not manage
// pf rules, so a fresh mesh TUN interface is reachable without any explicit
// allow rule, unlike a new Windows Wintun adapter. These no-ops keep
// the cross-platform call sites (Tunnel.SetMeshIP / Tunnel.Close) clean. The
// wgPort argument is accepted (and ignored) to match the Windows signature.
//
// Linux is not covered by this file - see firewall_linux.go. Unlike macOS,
// a stock Ubuntu/Pop!_OS/Debian desktop commonly ships with ufw active and a
// default-deny INPUT policy. ufw's stateful conntrack lets WireGuard's own
// UDP handshake/keepalive traffic through (it rides on the connection state
// created by this node's own outbound punch/keepalive), which is why a
// DERP/direct peer reads reachable=true - but the decrypted IP packets
// WireGuard delivers to the mesh TUN interface (ICMP, TCP SYNs, any real
// application traffic) are a separate flow from ufw's point of view and hit
// the default-deny INPUT policy with no matching allow rule, so they're
// silently dropped. A peer pair can show reachable=true / mode="direct" on
// both sides with fresh handshakes while ping and TCP see 100% loss in both
// directions: the WG-encrypted UDP round-trips on the wire but the peer's
// kernel drops the decrypted ICMP echo reply before it reaches the
// requesting process.

func allowMeshInbound(_ string, _ int) error  { return nil }
func removeMeshInbound(_ string, _ int) error { return nil }
