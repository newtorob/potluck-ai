//go:build !windows

package tunnel

// allowMeshInbound / removeMeshInbound are Windows-only. macOS and Linux do not
// block inbound on a fresh mesh TUN interface the way a new Windows Wintun
// adapter does, so there is nothing to open here -- these are no-ops that keep
// the cross-platform call sites (Tunnel.SetMeshIP / Tunnel.Close) clean.

func allowMeshInbound(_ string) error  { return nil }
func removeMeshInbound(_ string) error { return nil }
