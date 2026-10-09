//go:build meshlive_filter

// Harness-only variant of newPacketFilter. Dockerfile.sidecar copies this file
// into cmd/potluck-mesh/ of a scratch copy of the source (SHIM=filter) and
// builds with -tags meshlive_filter, which excludes the shipped
// packetfilter.go. Nothing here exists in the default build.
//
// It builds the real filter with the real API. What it adds is a way to make
// one household peer look like a circle peer, because no circle peer can exist
// yet:
//
//   - the household function it gives the filter says "not a household peer"
//     for the one marked address, as it would for a circle peer, whose address
//     is never in the roster store;
//   - SetEntry and RemoveEntry are called on that address through the filter's
//     own methods, driven over a unix socket by the harness.
//
// The packet path (wrapper, verdict, flow tracking, deadlines) is the shipped
// code unchanged.
package main

import (
	"encoding/json"
	"log/slog"
	"math/rand"
	"net"
	"net/http"
	"net/netip"
	"os"
	"strconv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"github.com/potluck-ai/potluck-mesh/internal/peerstate"
	"github.com/potluck-ai/potluck-mesh/internal/tunnel"
)

const shimSocket = "/run/filter-shim.sock"

type filterShim struct {
	ps     *peerstate.Store
	f      *tunnel.PacketFilter
	marked atomic.Uint32 // the address treated as not-household, 0 for none

	mu       sync.Mutex
	flapping bool
	flaps    int64
}

func (s *filterShim) household(a [4]byte) bool {
	if m := s.marked.Load(); m != 0 && m == uint32(a[0])<<24|uint32(a[1])<<16|uint32(a[2])<<8|uint32(a[3]) {
		return false
	}
	return s.ps.IsHousehold(a)
}

func (s *filterShim) mark(a netip.Addr) {
	b := a.As4()
	s.marked.Store(uint32(b[0])<<24 | uint32(b[1])<<16 | uint32(b[2])<<8 | uint32(b[3]))
}

func newPacketFilter(ps *peerstate.Store) *tunnel.PacketFilter {
	s := &filterShim{ps: ps}
	s.f = tunnel.NewPacketFilter(s.household)
	// MESHLIVE_FILTER=<peer>:<in port>:<out port>:<ttl seconds> restricts a peer
	// from the very first packet, so a restart cannot leave a window.
	if v := os.Getenv("MESHLIVE_FILTER"); v != "" {
		p := strings.Split(v, ":")
		addr, err := netip.ParseAddr(p[0])
		in, _ := strconv.Atoi(p[1])
		out, _ := strconv.Atoi(p[2])
		ttl, _ := strconv.Atoi(p[3])
		if err != nil || len(p) != 4 {
			slog.Error("MESHLIVE_FILTER is malformed", "value", v)
			os.Exit(2)
		}
		s.restrict(addr, uint16(in), uint16(out), time.Duration(ttl)*time.Second)
	}
	go s.serve()
	slog.Warn("MESHLIVE FILTER SHIM ACTIVE: harness build, one peer can be made restricted over " + shimSocket)
	return s.f
}

func (s *filterShim) restrict(a netip.Addr, in, out uint16, ttl time.Duration) error {
	s.mark(a)
	return s.f.SetEntry(a, tunnel.Grant{InPort: in, OutPort: out, TTL: ttl})
}

func (s *filterShim) serve() {
	_ = os.Remove(shimSocket)
	l, err := net.Listen("unix", shimSocket)
	if err != nil {
		slog.Error("filter shim: cannot listen", "err", err)
		return
	}
	mux := http.NewServeMux()
	reply := func(w http.ResponseWriter, v any) {
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(v)
	}
	peerArg := func(w http.ResponseWriter, r *http.Request) (netip.Addr, bool) {
		a, err := netip.ParseAddr(r.URL.Query().Get("peer"))
		if err != nil {
			http.Error(w, "bad peer", http.StatusBadRequest)
			return a, false
		}
		return a, true
	}
	num := func(r *http.Request, k string) int { n, _ := strconv.Atoi(r.URL.Query().Get(k)); return n }

	// GET /state: the entries and the drop counters, as the filter reports them.
	mux.HandleFunc("/state", func(w http.ResponseWriter, r *http.Request) {
		type entry struct {
			Peer      string  `json:"peer"`
			In        uint16  `json:"in"`
			Out       uint16  `json:"out"`
			Remaining float64 `json:"remaining_s"`
			Expired   bool    `json:"expired"`
			Flows     int     `json:"flows"`
		}
		var es []entry
		for _, e := range s.f.Entries() {
			es = append(es, entry{e.Peer.String(), e.InPort, e.OutPort, e.Remaining.Seconds(), e.Expired, e.Flows})
		}
		s.mu.Lock()
		flaps := s.flaps
		s.mu.Unlock()
		reply(w, map[string]any{"entries": es, "dropped": s.f.Stats().Dropped, "dropped_total": s.f.Stats().Total(), "flaps": flaps})
	})
	// GET /household?peer=: whether the roster store's address set (what the filter asks about
	// household peers, before this shim's override) holds the address right now.
	mux.HandleFunc("/household", func(w http.ResponseWriter, r *http.Request) {
		a, ok := peerArg(w, r)
		if !ok {
			return
		}
		reply(w, map[string]bool{"household": a.Is4() && s.ps.IsHousehold(a.As4())})
	})
	// POST /restrict?peer=&in=&out=&ttl=<seconds>: treat the peer as a circle peer and add its entry.
	mux.HandleFunc("/restrict", func(w http.ResponseWriter, r *http.Request) {
		a, ok := peerArg(w, r)
		if !ok {
			return
		}
		if err := s.restrict(a, uint16(num(r, "in")), uint16(num(r, "out")), time.Duration(num(r, "ttl"))*time.Second); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		reply(w, map[string]string{"ok": "restricted"})
	})
	// POST /entry: SetEntry only (renewal, new ports, a short ttl); DELETE /entry: RemoveEntry only.
	mux.HandleFunc("/entry", func(w http.ResponseWriter, r *http.Request) {
		a, ok := peerArg(w, r)
		if !ok {
			return
		}
		if r.Method == http.MethodDelete {
			s.f.RemoveEntry(a)
			reply(w, map[string]string{"ok": "removed"})
			return
		}
		if err := s.f.SetEntry(a, tunnel.Grant{InPort: uint16(num(r, "in")), OutPort: uint16(num(r, "out")), TTL: time.Duration(num(r, "ttl")) * time.Second}); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		reply(w, map[string]string{"ok": "set"})
	})
	// POST /release?peer=: remove the entry and give the peer back to the household.
	mux.HandleFunc("/release", func(w http.ResponseWriter, r *http.Request) {
		a, ok := peerArg(w, r)
		if !ok {
			return
		}
		s.f.RemoveEntry(a)
		s.marked.Store(0)
		reply(w, map[string]string{"ok": "released"})
	})
	// POST /flap?peer=&hz=&seconds=: change the peer's entry at random, hz times a
	// second, for that long, without ever granting more than port in (8080 for
	// the harness) inbound and out outbound. It returns at once.
	mux.HandleFunc("/flap", func(w http.ResponseWriter, r *http.Request) {
		a, ok := peerArg(w, r)
		if !ok {
			return
		}
		in, out, hz, secs := uint16(num(r, "in")), uint16(num(r, "out")), num(r, "hz"), num(r, "seconds")
		if hz < 1 || secs < 1 {
			http.Error(w, "hz and seconds are required", http.StatusBadRequest)
			return
		}
		s.mu.Lock()
		if s.flapping {
			s.mu.Unlock()
			http.Error(w, "already flapping", http.StatusConflict)
			return
		}
		s.flapping = true
		s.mu.Unlock()
		go func() {
			defer func() { s.mu.Lock(); s.flapping = false; s.mu.Unlock() }()
			end := time.Now().Add(time.Duration(secs) * time.Second)
			tick := time.NewTicker(time.Second / time.Duration(hz))
			defer tick.Stop()
			for time.Now().Before(end) {
				<-tick.C
				switch rand.Intn(5) {
				case 0:
					s.f.RemoveEntry(a)
				case 1:
					_ = s.f.SetEntry(a, tunnel.Grant{InPort: in, TTL: time.Duration(1+rand.Intn(30)) * time.Second})
				case 2:
					_ = s.f.SetEntry(a, tunnel.Grant{OutPort: out, TTL: time.Duration(1+rand.Intn(30)) * time.Second})
				default:
					_ = s.f.SetEntry(a, tunnel.Grant{InPort: in, OutPort: out, TTL: time.Duration(1+rand.Intn(2)) * time.Second})
				}
				s.mu.Lock()
				s.flaps++
				s.mu.Unlock()
			}
			// Leave the entry in the normal state when the flap is over.
			_ = s.f.SetEntry(a, tunnel.Grant{InPort: in, OutPort: out, TTL: time.Hour})
		}()
		reply(w, map[string]string{"ok": "flapping"})
	})
	_ = http.Serve(l, mux)
}
