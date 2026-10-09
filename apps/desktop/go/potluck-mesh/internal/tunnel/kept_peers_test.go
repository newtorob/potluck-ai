package tunnel

import (
	"bytes"
	"encoding/base64"
	"log/slog"
	"os"
	"strings"
	"testing"

	"golang.zx2c4.com/wireguard/conn"
	"golang.zx2c4.com/wireguard/device"
	"golang.zx2c4.com/wireguard/tun"

	"github.com/potluck-ai/potluck-mesh/internal/derp"
)

// These tests run a real wireguard-go device (never brought up, so it binds no
// port and sends nothing) on a fake TUN, to show what SetPeers does to peers
// that are not in its list.

func keyOf(n byte) string {
	b := make([]byte, 32)
	for i := range b {
		b[i] = n
	}
	return base64.StdEncoding.EncodeToString(b)
}

// closableTUN is the fake TUN that device.Close can stop: Read blocks until
// Close, then reports the device closed, and the event channel closes with it.
type closableTUN struct {
	*fakeTUN
	ev chan tun.Event
}

func (d *closableTUN) Events() <-chan tun.Event { return d.ev }
func (d *closableTUN) Read([][]byte, []int, int) (int, error) {
	<-d.ev // closed by Close
	return 0, os.ErrClosed
}
func (d *closableTUN) Close() error { close(d.ev); return d.fakeTUN.Close() }

func newDownTunnel(t *testing.T) *Tunnel {
	t.Helper()
	return newDownTunnelWith(t, conn.NewDefaultBind())
}

func newDownTunnelWith(t *testing.T, bind conn.Bind) *Tunnel {
	t.Helper()
	dev := device.NewDevice(&closableTUN{fakeTUN: newFakeTUN(t, profLinux), ev: make(chan tun.Event)}, bind, newWGLogger())
	t.Cleanup(dev.Close)
	tn := &Tunnel{dev: dev, ifName: "fake0", wgPort: wgPort}
	if err := dev.IpcSet("private_key=" + strings.Repeat("11", 32) + "\n"); err != nil {
		t.Fatal(err)
	}
	return tn
}

func household(n byte, ip string) Peer {
	return Peer{PublicKey: keyOf(n), AllowedIPs: []string{ip + "/32"}, PersistentKeepalive: 25}
}

func circle(n byte, ip string) Peer {
	return Peer{PublicKey: keyOf(n), AllowedIPs: []string{ip + "/32"}, PersistentKeepalive: 25}
}

func configured(t *testing.T, tn *Tunnel, key string) bool {
	t.Helper()
	ok, err := tn.IsPeerConfigured(key)
	if err != nil {
		t.Fatal(err)
	}
	return ok
}

// The problem the kept peers solve: a welcome calls SetPeers with the roster
// only, and replace_peers removes the circle peer that was added with AddPeer.
func TestSetPeersWithoutKeptPeersDropsAnAddedPeer(t *testing.T) {
	tn := newDownTunnel(t)
	if err := tn.AddPeer(circle(2, "100.64.1.2")); err != nil {
		t.Fatal(err)
	}
	if err := tn.SetPeers([]Peer{household(1, "100.64.0.7")}); err != nil {
		t.Fatal(err)
	}
	if configured(t, tn, keyOf(2)) {
		t.Fatal("setup: replace_peers was expected to remove the extra peer; the test would prove nothing")
	}
}

func TestSetPeersKeepsTheKeptPeers(t *testing.T) {
	tn := newDownTunnel(t)
	kept := []Peer{circle(2, "100.64.1.2")}
	tn.SetKeptPeers(func() []Peer { return kept })
	if err := tn.AddPeer(kept[0]); err != nil {
		t.Fatal(err)
	}
	if err := tn.SetPeers([]Peer{household(1, "100.64.0.7")}); err != nil {
		t.Fatal(err)
	}
	if !configured(t, tn, keyOf(1)) || !configured(t, tn, keyOf(2)) {
		t.Fatalf("after a welcome: household=%v circle=%v, want both", configured(t, tn, keyOf(1)), configured(t, tn, keyOf(2)))
	}
	// A welcome with an empty roster still keeps them.
	if err := tn.SetPeers(nil); err != nil {
		t.Fatal(err)
	}
	if configured(t, tn, keyOf(1)) {
		t.Error("the household peer survived an empty roster")
	}
	if !configured(t, tn, keyOf(2)) {
		t.Error("an empty roster dropped the circle peer")
	}
}

// What the source stops returning is removed, so a peer that expired or ended is
// not brought back by a welcome.
func TestSetPeersDoesNotResurrectWhatTheSourceNoLongerReturns(t *testing.T) {
	tn := newDownTunnel(t)
	live := []Peer{circle(2, "100.64.1.2")}
	tn.SetKeptPeers(func() []Peer { return live })
	if err := tn.SetPeers([]Peer{household(1, "100.64.0.7")}); err != nil {
		t.Fatal(err)
	}
	if !configured(t, tn, keyOf(2)) {
		t.Fatal("the kept peer was not configured")
	}
	live = nil
	if err := tn.SetPeers([]Peer{household(1, "100.64.0.7")}); err != nil {
		t.Fatal(err)
	}
	if configured(t, tn, keyOf(2)) {
		t.Fatal("a peer the source no longer returns was carried over")
	}
	tn.SetKeptPeers(nil)
	if err := tn.SetPeers(nil); err != nil {
		t.Fatal(err)
	}
	if configured(t, tn, keyOf(1)) {
		t.Fatal("the household peer survived with no kept source and an empty roster")
	}
}

// A kept peer that shares a key or an allowed-ip with a roster peer is dropped:
// the roster wins, and wireguard-go would otherwise give the key or the address
// to whichever was configured last.
func TestKeptPeerThatClashesWithTheRosterIsDropped(t *testing.T) {
	tn := newDownTunnel(t)
	tn.SetKeptPeers(func() []Peer {
		return []Peer{circle(2, "100.64.1.2"), circle(3, "100.64.0.7"), circle(1, "100.64.9.9")}
	})
	if err := tn.SetPeers([]Peer{household(1, "100.64.0.7")}); err != nil {
		t.Fatal(err)
	}
	if !configured(t, tn, keyOf(1)) {
		t.Fatal("the roster peer is missing")
	}
	if configured(t, tn, keyOf(3)) {
		t.Error("a kept peer on a roster peer's address was configured")
	}
	if !configured(t, tn, keyOf(2)) {
		t.Error("an unrelated kept peer was dropped")
	}
	// The roster peer kept its own address and key.
	uapi, _ := tn.dev.IpcGet()
	if strings.Count(uapi, "allowed_ip=100.64.9.9/32") != 0 {
		t.Error("the roster peer's key was given the kept peer's address")
	}
}

// A circle peer's endpoint is a stranger's address, and the relay endpoint spells
// out the stranger's key. Neither reaches the log at info level; a household
// peer's line is unchanged.
func TestPrivatePeerEndpointIsNotLoggedAtInfoButAHouseholdPeersIs(t *testing.T) {
	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, &slog.HandlerOptions{Level: slog.LevelInfo})))
	defer slog.SetDefault(prev)

	tn := newDownTunnelWith(t, derp.NewDerpBind(conn.NewDefaultBind(), nil)) // understands the relay endpoint
	synthetic := "derp:" + strings.Repeat("ab", 32)
	for i, ep := range []string{"203.0.113.9:51820", synthetic} {
		p := Peer{PublicKey: keyOf(byte(0x70 + i)), AllowedIPs: []string{"100.64.1.2/32"}, Endpoint: ep, PersistentKeepalive: 5, Private: true}
		if err := tn.AddPeer(p); err != nil {
			t.Fatal(err)
		}
	}
	if err := tn.SetPeers([]Peer{{PublicKey: keyOf(0x72), AllowedIPs: []string{"100.64.1.9/32"}, Endpoint: "bad endpoint\nallowed_ip=0.0.0.0/0", Private: true}}); err != nil {
		t.Fatal(err)
	}
	out := buf.String()
	if !strings.Contains(out, "tunnel: peer added") {
		t.Fatalf("no 'peer added' line:\n%s", out)
	}
	for _, secret := range []string{"203.0.113.9", "abab", "bad endpoint", "allowed_ip=0.0.0.0"} {
		if strings.Contains(out, secret) {
			t.Errorf("the info log contains %q:\n%s", secret, out)
		}
	}

	buf.Reset()
	if err := tn.AddPeer(Peer{PublicKey: keyOf(0x73), AllowedIPs: []string{"100.64.0.7/32"}, Endpoint: "203.0.113.50:51820"}); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(buf.String(), "203.0.113.50:51820") {
		t.Errorf("a household peer's log line changed:\n%s", buf.String())
	}
}
