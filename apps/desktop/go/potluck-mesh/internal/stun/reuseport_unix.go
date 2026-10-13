//go:build !windows

package stun

import (
	"context"
	"net"
	"strconv"
	"syscall"

	"golang.org/x/sys/unix"
)

// listenReusable binds a UDP socket to ":port" on the given network ("udp4"
// or "udp6") with SO_REUSEADDR and SO_REUSEPORT set before bind, so it can
// coexist with wireguard-go's own socket already bound to the same port
// (internal/tunnel's wgPort - see tunnel.go). Both Linux and Darwin support
// SO_REUSEPORT for UDP: the kernel load-balances/duplicates delivery across
// reuse-group members by a hash of the packet, which is exactly what we want
// here - our probe socket only cares about the response to ITS OWN request
// (matched by the STUN transaction ID / read on our own fd), so which
// physical socket the kernel happens to route an unrelated inbound WG packet
// to is irrelevant.
//
// Returns an error (never a partially-bound conn) if either socket option
// fails to set or the bind itself fails - bindProbeConn's caller falls back
// to an ephemeral port in that case.
func listenReusable(network string, port int) (*net.UDPConn, error) {
	lc := net.ListenConfig{
		Control: func(_, _ string, c syscall.RawConn) error {
			var sockErr error
			ctrlErr := c.Control(func(fd uintptr) {
				if err := unix.SetsockoptInt(int(fd), unix.SOL_SOCKET, unix.SO_REUSEADDR, 1); err != nil {
					sockErr = err
					return
				}
				if err := unix.SetsockoptInt(int(fd), unix.SOL_SOCKET, unix.SO_REUSEPORT, 1); err != nil {
					sockErr = err
					return
				}
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
