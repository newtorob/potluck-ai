package stun

import "errors"

// errNotUDPConn is returned by the platform-specific listenReusable
// implementations if net.ListenConfig.ListenPacket somehow returns a
// net.PacketConn that isn't a *net.UDPConn for a "udp4"/"udp6" network —
// should never happen in practice, but bindProbeConn's caller treats it like
// any other bind failure (falls back to an ephemeral-port conn) rather than
// risk a type-assertion panic.
var errNotUDPConn = errors.New("stun: ListenPacket did not return a *net.UDPConn")
