//go:build darwin

package tunnel

// allowMeshInbound / removeMeshInbound are no-ops on macOS: macOS's packet
// filter (pf) is disabled by default (confirmed live: `pfctl -s all` reports
// "Status: Disabled" on a stock install) and the mesh code does not manage
// pf rules, so a fresh mesh TUN interface is reachable without any explicit
// allow rule the way a new Windows Wintun adapter is NOT. These no-ops keep
// the cross-platform call sites (Tunnel.SetMeshIP / Tunnel.Close) clean. The
// wgPort argument is accepted (and ignored) to match the Windows signature.
//
// Linux is NOT covered by this file — see firewall_linux.go. It used to share
// this same no-op stub (build tag `!windows`), which was wrong: unlike macOS,
// a stock Ubuntu/Pop!_OS/Debian desktop commonly ships with ufw active and a
// default-deny INPUT policy. ufw's stateful conntrack lets WireGuard's own
// UDP handshake/keepalive traffic through (it rides on the connection state
// created by this node's own outbound punch/keepalive), which is why a
// DERP/direct peer reads reachable=true — but the DECRYPTED IP packets
// WireGuard delivers to the mesh TUN interface (ICMP, TCP SYNs, any real
// application traffic) are a separate flow from ufw's point of view and hit
// the default-deny INPUT policy with no matching ALLOW rule, so they're
// silently dropped. Reproduced live against prod household 00000000,
// 2026-08-14: a Mac<->Linux peer pair showed reachable=true / mode="direct"
// on both sides with fresh handshakes, yet ping and TCP were 100% loss in
// BOTH directions — traced via tcpdump to the WG-encrypted UDP round-tripping
// perfectly on the wire while the peer's kernel silently ate the decrypted
// ICMP echo reply before it ever reached the requesting process.

func allowMeshInbound(_ string, _ int) error  { return nil }
func removeMeshInbound(_ string, _ int) error { return nil }
