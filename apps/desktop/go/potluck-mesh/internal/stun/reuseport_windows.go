//go:build windows

package stun

import (
	"context"
	"net"
	"strconv"
	"syscall"
)

// listenReusable binds a UDP socket to ":port" with SO_REUSEADDR set before
// bind, so it can coexist with wireguard-go's own socket already bound to the
// same port (internal/tunnel's wgPort — see tunnel.go).
//
// Windows has no SO_REUSEPORT; SO_REUSEADDR is the closest primitive and
// behaves differently than on Linux/Darwin (see reuseport_unix.go) — multiple
// sockets CAN bind the same UDP port, but inbound-delivery semantics across
// them are not the clean hash-based fan-out SO_REUSEPORT gives on Unix. That
// is acceptable here: this socket only exists for the brief lifetime of a
// single STUN request/response exchange, immediately followed by Close() (see
// bindProbeConn's callers), so any window where it competes with the
// long-lived WG socket for an inbound datagram is measured in milliseconds.
// If the bind fails outright (e.g. some Windows version/config rejects the
// same-port bind even with SO_REUSEADDR), bindProbeConn's caller falls back
// to an ephemeral port gracefully.
func listenReusable(network string, port int) (*net.UDPConn, error) {
	lc := net.ListenConfig{
		Control: func(_, _ string, c syscall.RawConn) error {
			var sockErr error
			ctrlErr := c.Control(func(fd uintptr) {
				sockErr = syscall.SetsockoptInt(syscall.Handle(fd), syscall.SOL_SOCKET, syscall.SO_REUSEADDR, 1)
			})
			if ctrlErr != nil {
				return ctrlErr
			}
			return sockErr
		},
	}

	addr := net.JoinHostPort("", strconv.Itoa(port))
	pc, err := lc.ListenPacket(context.Background(), network, addr)
	if err != nil {
		return nil, err
	}
	udpConn, ok := pc.(*net.UDPConn)
	if !ok {
		pc.Close()
		return nil, errNotUDPConn
	}
	return udpConn, nil
}
