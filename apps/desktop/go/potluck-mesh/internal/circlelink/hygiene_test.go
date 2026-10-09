package circlelink

import (
	"bytes"
	"encoding/base64"
	"encoding/hex"
	"fmt"
	"log/slog"
	"net/netip"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/potluck-ai/potluck-mesh/internal/peerstate"
	"github.com/potluck-ai/potluck-mesh/internal/signal"
	"github.com/potluck-ai/potluck-mesh/internal/tunnel"
)

// capture installs a log handler at the given level for one test.
func capture(t *testing.T, level slog.Level) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: level})))
	t.Cleanup(func() { slog.SetDefault(prev) })
	return &buf
}

// Through a whole link and its teardown, at the level the sidecar logs at in
// production: no WireGuard key, no token hash, no public endpoint of the other
// machine. The mesh address and the link id are fine (the filter already logs
// the mesh address, and the link id is a random id).
func TestLogsCarryNoKeyTokenOrPublicEndpointAtInfo(t *testing.T) {
	buf := capture(t, slog.LevelInfo)
	e := newEnv(t)
	k := keyN(80)
	m := withDirect(addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 600), wanEP, 3000)
	mustReady(t, e.add(m), signal.CircleOpAdd)
	e.clk.advance(4 * time.Second)
	e.c.Sweep() // falls back to the relay
	mustReady(t, e.extend(linkA, 600, sp("198.51.100.4:51820")), signal.CircleOpExtend)
	// Provoke the refusals that log.
	bad := addMsg(linkB, signal.CircleRoleLender, keyN(81), hhIP, 8347, 600)
	e.add(bad)
	bad2 := addMsg(linkC, signal.CircleRoleLender, "not a key\nforged=line", circIP2, 8347, 600)
	e.add(bad2)
	e.remove(linkA, "evil\nreason=injected")
	e.c.HouseholdChanged("", nil)
	e.c.Clear()

	out := buf.String()
	if out == "" {
		t.Fatal("nothing was logged")
	}
	for _, secret := range []string{k, k[:12], tokenA, tokenA[:16], "203.0.113.9", "198.51.100.4", "not a key", "forged=line", "reason=injected", synthetic(k)} {
		if strings.Contains(out, secret) {
			t.Errorf("the info log contains %q:\n%s", secret, out)
		}
	}
	// The truncated prefix form the rest of the sidecar uses is allowed, nothing longer.
	for _, line := range strings.Split(out, "\n") {
		if strings.Contains(line, "\n") {
			t.Errorf("a log line spans lines: %q", line)
		}
	}
}

// The sidecar writes debug lines to the same file as the rest, so the endpoint
// stays out of them too: no level of the log names it, and none names the key or
// the token hash.
func TestDebugLogNeverNamesTheEndpointKeyOrToken(t *testing.T) {
	buf := capture(t, slog.LevelDebug)
	e := newEnv(t)
	k := keyN(82)
	mustReady(t, e.add(withDirect(addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 600), wanEP, 3000)), signal.CircleOpAdd)
	e.clk.advance(1600 * time.Millisecond)
	e.c.Sweep()
	e.clk.advance(2 * time.Second)
	e.c.Sweep()
	e.remove(linkA, signal.CircleRemoveEnded)
	out := buf.String()
	if !strings.Contains(out, "circle link:") {
		t.Fatal("nothing was logged")
	}
	for _, secret := range []string{k, tokenA, "203.0.113.9", synthetic(k)} {
		if strings.Contains(out, secret) {
			t.Errorf("the debug log contains %q:\n%s", secret, out)
		}
	}
}

// With the real packet filter: the entry the controller installs is the one the
// filter reports, it carries the granted ports, it refuses at the deadline on its
// own, and the controller then removes it.
func TestRealFilterEntryFollowsTheLink(t *testing.T) {
	log := &opLog{}
	tun := newFakeTun(log)
	ps := peerstate.New()
	ps.Reset(selfIP, nil)
	flt := tunnel.NewPacketFilter(ps.IsHousehold)
	flt.SetSelf(netip.MustParseAddr(selfIP).AsSlice())
	c := New(Config{
		Tunnel: tun, Filter: flt, Signal: &fakeSignal{log: log}, Household: ps,
		Self: func() (netip.Addr, string) { return netip.MustParseAddr(selfIP), selfKey },
	})
	k := keyN(83)
	c.OnCirclePeerAdd(addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 1))
	got := flt.Entries()
	if len(got) != 1 || got[0].Peer.String() != circIP1 || got[0].InPort != 8347 || got[0].OutPort != 0 || got[0].Expired {
		t.Fatalf("filter entries = %+v", got)
	}
	time.Sleep(1100 * time.Millisecond)
	if got := flt.Entries(); len(got) != 1 || !got[0].Expired {
		t.Fatalf("the filter did not refuse at the deadline on its own: %+v", got)
	}
	c.Sweep() // the controller's own clock tears down
	if got := flt.Entries(); len(got) != 0 {
		t.Errorf("filter entries after the sweep = %+v", got)
	}
	if tun.hasPeer(k) {
		t.Error("the device peer outlived the link")
	}
	// A second side on the same peer shows in the one entry.
	c.OnCirclePeerAdd(addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 600))
	c.OnCirclePeerAdd(addMsg(linkB, signal.CircleRoleRequester, k, circIP1, 8340, 600))
	if got := flt.Entries(); len(got) != 1 || got[0].InPort != 8347 || got[0].OutPort != 8340 {
		t.Errorf("merged filter entry = %+v", got)
	}
}

// Everything at once, under the race detector: messages, sweeps, welcome-time
// snapshot reads and view reads from several goroutines.
func TestConcurrentUseIsRaceFree(t *testing.T) {
	e := newEnv(t)
	var wg sync.WaitGroup
	stop := make(chan struct{})
	for i := 0; i < 3; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case <-stop:
					return
				default:
					_ = e.c.Kept()
					_ = e.c.Peers()
				}
			}
		}()
	}
	wg.Add(1)
	go func() {
		defer wg.Done()
		for {
			select {
			case <-stop:
				return
			default:
				e.c.Sweep()
				e.clk.advance(50 * time.Millisecond)
			}
		}
	}()
	for round := 0; round < 40; round++ {
		for i := 0; i < 4; i++ {
			link := fmt.Sprintf("c0ffee00-1111-4222-8333-%012d", i)
			e.c.OnCirclePeerAdd(addMsg(link, signal.CircleRoleLender, keyN(byte(150+i)), fmt.Sprintf("100.64.%d.2", 10+i), 8347, 5))
			e.c.OnCirclePeerExtend(signal.CirclePeerExtend{Type: "circle_peer_extend", LinkID: link, TTLSeconds: 5})
		}
		e.c.HouseholdChanged("", nil)
		for i := 0; i < 4; i++ {
			e.c.OnCirclePeerRemove(signal.CirclePeerRemove{Type: "circle_peer_remove", LinkID: fmt.Sprintf("c0ffee00-1111-4222-8333-%012d", i), Reason: "ended"})
		}
	}
	e.c.Clear()
	close(stop)
	wg.Wait()
}

// The tunnel's errors quote the key in full, in base64 and in hex, and the
// endpoint. The controller logs them with all three cut out.
func TestTunnelErrorsAreScrubbedBeforeTheyAreLogged(t *testing.T) {
	buf := capture(t, slog.LevelDebug)
	e := newEnv(t)
	k := keyN(84)
	mustReady(t, e.add(withDirect(addMsg(linkA, signal.CircleRoleLender, k, circIP1, 8347, 600), wanEP, 3000)), signal.CircleOpAdd)
	raw, _ := base64.StdEncoding.DecodeString(k)
	hexKey := hex.EncodeToString(raw)

	quote := func(op string) error {
		return fmt.Errorf("tunnel: %s %s: IPC error -22: public_key=%s endpoint=%s\nforged=line", op, k, hexKey, wanEP)
	}
	e.tun.failOn("RemovePeer", quote("remove peer"))
	e.tun.failOn("AddPeer", quote("add peer"))
	// Provoke each logging site: the window ends (flip), a poke, an endpoint
	// follow, a removal.
	e.clk.advance(1600 * time.Millisecond)
	e.c.Sweep()
	e.clk.advance(2 * time.Second)
	e.c.Sweep()
	e.extend(linkA, 600, sp("198.51.100.4:51820"))
	e.remove(linkA, signal.CircleRemoveEnded)

	out := buf.String()
	if !strings.Contains(out, "could not") {
		t.Fatalf("no error line was logged; the test proves nothing:\n%s", out)
	}
	for _, secret := range []string{k, hexKey} {
		if strings.Contains(out, secret) {
			t.Errorf("the log contains %q", secret)
		}
	}
	for _, line := range strings.Split(out, "\n") {
		if strings.HasPrefix(line, "forged") {
			t.Errorf("a newline in an error forged a log line: %q", line)
		}
	}
	for _, line := range strings.Split(out, "\n") {
		if strings.Contains(line, "level=INFO") || strings.Contains(line, "level=WARN") || strings.Contains(line, "level=ERROR") {
			if strings.Contains(line, "203.0.113.9") || strings.Contains(line, "198.51.100.4") {
				t.Errorf("an info, warn or error line carries the endpoint: %s", line)
			}
		}
	}
}
