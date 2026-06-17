//go:build windows

package lanpair

import "syscall"

// enableBroadcast sets SO_BROADCAST on the raw socket fd so it may send to IPv4
// broadcast addresses. Without it the kernel rejects such sends.
func enableBroadcast(fd uintptr) error {
	return syscall.SetsockoptInt(syscall.Handle(fd), syscall.SOL_SOCKET, syscall.SO_BROADCAST, 1)
}
