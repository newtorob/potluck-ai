//go:build meshlive_portfilter

// Throwaway prototype of the circle-link port filter. Dockerfile.sidecar copies
// it into a scratch copy of internal/tunnel (PROTO=portfilter); the shipped
// source and default build never contain it. It wraps the tun.Device that
// wireguard-go reads and writes, so it sees every plain IP packet whatever the
// transport (direct, LAN, DERP) and the OS. PORTFILTER_PEERS lists circle peer
// mesh addresses; unset, the TUN is returned untouched. Left out on purpose:
// IPv6, a check that the destination is this host, connection tracking.
package tunnel

import (
	"encoding/binary"
	"fmt"
	"log/slog"
	"net/netip"
	"os"
	"strconv"
	"strings"
	"sync/atomic"

	"golang.zx2c4.com/wireguard/tun"
)

type portFilter struct {
	tun.Device
	peers  map[[4]byte]bool // circle peers by mesh address
	local  uint16           // the one TCP port on this host a circle peer may reach
	remote uint16           // the one TCP port on a circle peer this host may reach
	drops  atomic.Uint64
}

func createTUNFiltered(name string, mtu int) (tun.Device, error) {
	dev, err := tun.CreateTUN(name, mtu)
	if err != nil || os.Getenv("PORTFILTER_PEERS") == "" {
		return dev, err
	}
	f := &portFilter{Device: dev, peers: map[[4]byte]bool{}}
	for _, s := range strings.Split(os.Getenv("PORTFILTER_PEERS"), ",") {
		a, perr := netip.ParseAddr(strings.TrimSpace(s))
		if perr != nil || !a.Is4() {
			dev.Close()
			return nil, fmt.Errorf("portfilter: bad PORTFILTER_PEERS entry %q", s)
		}
		f.peers[a.As4()] = true
	}
	l, _ := strconv.Atoi(os.Getenv("PORTFILTER_LOCAL_PORT"))
	r, _ := strconv.Atoi(os.Getenv("PORTFILTER_REMOTE_PORT"))
	f.local, f.remote = uint16(l), uint16(r)
	slog.Info("PORT FILTER PROTOTYPE ACTIVE", "peers", len(f.peers), "local_port", f.local, "remote_port", f.remote)
	return f, nil
}

// Read gets packets from this host heading into the tunnel. A dropped packet
// gets size 0, which wireguard-go's TUN reader skips.
func (f *portFilter) Read(bufs [][]byte, sizes []int, offset int) (int, error) {
	n, err := f.Device.Read(bufs, sizes, offset)
	for i := 0; i < n; i++ {
		if !f.allow(bufs[i][offset:offset+sizes[i]], false) {
			sizes[i] = 0
		}
	}
	return n, err
}

// Write gets packets that came out of the tunnel toward this host; dropped ones
// leave the batch. wireguard-go has already checked each source address against
// the sending peer's allowed-ips.
func (f *portFilter) Write(bufs [][]byte, offset int) (int, error) {
	kept := bufs[:0]
	for _, b := range bufs {
		if f.allow(b[offset:], true) {
			kept = append(kept, b)
		}
	}
	if len(kept) == 0 {
		return len(bufs), nil
	}
	return f.Device.Write(kept, offset)
}

// allow is the whole policy. in is true for a packet from a peer to this host.
func (f *portFilter) allow(p []byte, in bool) bool {
	if len(p) > 0 && p[0]>>4 == 6 {
		return true // IPv6: the mesh has no IPv6 allowed-ips, so no circle peer is on it
	}
	if len(p) < 20 || p[0]>>4 != 4 {
		return f.drop() // cannot parse it, so cannot rule a circle peer out
	}
	var peer [4]byte
	copy(peer[:], p[16:20]) // the peer is the destination of an outgoing packet
	if in {
		copy(peer[:], p[12:16]) // and the source of an incoming one
	}
	if !f.peers[peer] {
		return true
	}
	ihl := int(p[0]&0xf) * 4
	// TCP only, whole header present, no fragments (later ones carry no ports).
	if p[9] != 6 || ihl < 20 || len(p) < ihl+14 || binary.BigEndian.Uint16(p[6:8])&0x3fff != 0 {
		return f.drop()
	}
	sport, dport := binary.BigEndian.Uint16(p[ihl:]), binary.BigEndian.Uint16(p[ihl+2:])
	opens := p[ihl+13]&0x12 == 0x02 // SYN without ACK starts a connection
	ok := dport == f.remote || (sport == f.local && !opens)
	if in {
		ok = dport == f.local || (sport == f.remote && !opens)
	}
	return ok || f.drop()
}

func (f *portFilter) drop() bool {
	if n := f.drops.Add(1); n <= 20 {
		slog.Info("portfilter: dropped a packet", "total", n)
	}
	return false
}
