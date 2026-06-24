//go:build !windows

package tunnel

// allowMeshInbound / removeMeshInbound are Windows-only. macOS and Linux do not
// block inbound on a fresh mesh TUN interface the way a new Windows Wintun
// adapter does, and the WG transport port is handled by a separate service on
// Linux, so there is nothing to open here -- these are no-ops that keep the
// cross-platform call sites (Tunnel.SetMeshIP / Tunnel.Close) clean. The wgPort
// argument is accepted (and ignored) to match the Windows signature.

func allowMeshInbound(_ string, _ int) error  { return nil }
func removeMeshInbound(_ string, _ int) error { return nil }
