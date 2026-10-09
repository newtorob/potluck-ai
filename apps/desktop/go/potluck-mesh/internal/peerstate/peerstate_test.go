package peerstate

import (
	"bytes"
	"encoding/base64"
	"fmt"
	"log/slog"
	"net/netip"
	"sort"
	"strings"
	"sync"
	"testing"

	"github.com/potluck-ai/potluck-mesh/internal/signal"
)

// testKey returns a canonical base64 WireGuard public key (32 bytes of seed).
func testKey(seed byte) string {
	b := make([]byte, 32)
	for i := range b {
		b[i] = seed
	}
	return base64.StdEncoding.EncodeToString(b)
}

func peer(id, ip string, seed byte) signal.RosterPeer {
	return signal.RosterPeer{NodeID: id, NodeName: id, MeshIP: ip, WGPublicKey: testKey(seed)}
}

func ids(peers []signal.RosterPeer) []string {
	out := make([]string, 0, len(peers))
	for _, p := range peers {
		out = append(out, p.NodeID)
	}
	return out
}

func storedIDs(s *Store) []string {
	out := ids(s.All())
	sort.Strings(out)
	return out
}

func rejectedIDs(r []Rejection) []string {
	out := make([]string, 0, len(r))
	for _, x := range r {
		out = append(out, x.NodeID)
	}
	sort.Strings(out)
	return out
}

func eq(a, b []string) bool {
	if len(a) != len(b) {
		return false
	}
	for i := range a {
		if a[i] != b[i] {
			return false
		}
	}
	return true
}

// captureLogs routes slog output into a buffer for the duration of the test.
func captureLogs(t *testing.T) *bytes.Buffer {
	t.Helper()
	var buf bytes.Buffer
	prev := slog.Default()
	slog.SetDefault(slog.New(slog.NewTextHandler(&buf, nil)))
	t.Cleanup(func() { slog.SetDefault(prev) })
	return &buf
}

var (
	poolRange = netip.MustParsePrefix("100.64.0.0/10")
	selfAddr  = netip.MustParseAddr("100.64.0.6")
)

func TestValidateMeshIP_Table(t *testing.T) {
	tests := []struct {
		name    string
		ip      string
		mesh    netip.Prefix
		self    netip.Addr
		wantErr string // substring of the error; "" means valid
	}{
		// valid
		{"household host", "100.64.0.5", poolRange, selfAddr, ""},
		{"first pool host", "100.64.0.1", poolRange, selfAddr, ""},
		{"last pool host", "100.127.255.254", poolRange, selfAddr, ""},
		{"other household slice", "100.100.7.9", poolRange, selfAddr, ""},
		{"no self known", "100.64.0.6", poolRange, netip.Addr{}, ""},
		{"custom range host", "10.42.0.7", netip.MustParsePrefix("10.42.0.0/16"), selfAddr, ""},

		// not a plain IPv4 address
		{"prefix", "100.64.0.5/32", poolRange, selfAddr, "not a plain IPv4"},
		{"port", "100.64.0.5:80", poolRange, selfAddr, "not a plain IPv4"},
		{"leading space", " 100.64.0.5", poolRange, selfAddr, "not a plain IPv4"},
		{"trailing space", "100.64.0.5 ", poolRange, selfAddr, "not a plain IPv4"},
		{"trailing newline", "100.64.0.5\n", poolRange, selfAddr, "not a plain IPv4"},
		{"leading tab", "\t100.64.0.5", poolRange, selfAddr, "not a plain IPv4"},
		{"embedded NUL", "100.64.0.5\x00", poolRange, selfAddr, "not a plain IPv4"},
		{"uapi injection", "100.64.0.5\nallowed_ip=0.0.0.0/0", poolRange, selfAddr, "not a plain IPv4"},
		{"zero width space", "100.64.0.5\u200b", poolRange, selfAddr, "not a plain IPv4"},
		{"unicode digits", "\u0661\u0660\u0660.64.0.5", poolRange, selfAddr, "not a plain IPv4"},
		{"empty", "", poolRange, selfAddr, "not a plain IPv4"},
		{"300 chars", strings.Repeat("1", 300), poolRange, selfAddr, "not a plain IPv4"},
		{"300 chars dotted", "100.64.0." + strings.Repeat("5", 291), poolRange, selfAddr, "not a plain IPv4"},
		{"leading zero octet", "100.064.0.5", poolRange, selfAddr, "not a plain IPv4"},
		{"three octets", "100.64.5", poolRange, selfAddr, "not a plain IPv4"},
		{"five octets", "100.64.0.5.1", poolRange, selfAddr, "not a plain IPv4"},
		{"hex octet", "0x64.0.0.5", poolRange, selfAddr, "not a plain IPv4"},
		{"decimal integer", "1684275205", poolRange, selfAddr, "not a plain IPv4"},
		{"ipv6 link-local", "fe80::1", poolRange, selfAddr, "not a plain IPv4"},
		{"ipv6 with zone", "fe80::1%eth0", poolRange, selfAddr, "not a plain IPv4"},
		{"ipv6 loopback", "::1", poolRange, selfAddr, "not a plain IPv4"},
		{"ipv4-mapped ipv6", "::ffff:100.64.0.5", poolRange, selfAddr, "not a plain IPv4"},

		// special addresses (checked ahead of the range so they stay refused
		// even under a broad POTLUCK_MESH_CIDR)
		{"unspecified", "0.0.0.0", poolRange, selfAddr, "unspecified"},
		{"unspecified broad range", "0.0.0.0", netip.MustParsePrefix("0.0.0.0/0"), selfAddr, "unspecified"},
		{"loopback", "127.0.0.1", poolRange, selfAddr, "loopback"},
		{"loopback broad range", "127.0.0.1", netip.MustParsePrefix("0.0.0.0/0"), selfAddr, "loopback"},
		{"multicast", "224.0.0.1", poolRange, selfAddr, "multicast"},
		{"multicast broad range", "239.1.2.3", netip.MustParsePrefix("0.0.0.0/0"), selfAddr, "multicast"},
		{"link-local", "169.254.7.7", poolRange, selfAddr, "link-local"},
		{"link-local broad range", "169.254.7.7", netip.MustParsePrefix("0.0.0.0/0"), selfAddr, "link-local"},

		// range
		{"lan gateway", "192.168.1.1", poolRange, selfAddr, "outside the mesh range"},
		{"public address", "8.8.8.8", poolRange, selfAddr, "outside the mesh range"},
		{"10/8 address", "10.42.0.7", poolRange, selfAddr, "outside the mesh range"},
		{"just above the pool", "100.128.0.1", poolRange, selfAddr, "outside the mesh range"},
		{"just below the pool", "100.63.255.255", poolRange, selfAddr, "outside the mesh range"},
		{"pool address outside custom range", "100.64.0.5", netip.MustParsePrefix("10.42.0.0/16"), selfAddr, "outside the mesh range"},
		{"ipv6 range never matches", "100.64.0.5", netip.MustParsePrefix("fd00::/8"), selfAddr, "outside the mesh range"},
		{"invalid range matches nothing", "100.64.0.5", netip.Prefix{}, selfAddr, "outside the mesh range"},

		// the range's broadcast address
		{"pool broadcast", "100.127.255.255", poolRange, selfAddr, "broadcast"},
		{"custom range broadcast", "10.42.255.255", netip.MustParsePrefix("10.42.0.0/16"), selfAddr, "broadcast"},
		{"limited broadcast under broad range", "255.255.255.255", netip.MustParsePrefix("0.0.0.0/0"), selfAddr, "broadcast"},

		// this node's own address
		{"own address", "100.64.0.6", poolRange, selfAddr, "own address"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := validateMeshIP(tt.ip, tt.mesh, tt.self)
			if tt.wantErr == "" {
				if err != nil {
					t.Fatalf("validateMeshIP(%q) = %v, want ok", tt.ip, err)
				}
				if got.String() != tt.ip {
					t.Fatalf("parsed %q as %s", tt.ip, got)
				}
				return
			}
			if err == nil {
				t.Fatalf("validateMeshIP(%q) accepted %s, want error containing %q", tt.ip, got, tt.wantErr)
			}
			if !strings.Contains(err.Error(), tt.wantErr) {
				t.Fatalf("validateMeshIP(%q) error %q, want it to contain %q", tt.ip, err, tt.wantErr)
			}
			// The reason goes to the logs, so it must not echo hostile input.
			if len(err.Error()) > 120 || strings.ContainsAny(err.Error(), "\n\r\x00") {
				t.Fatalf("error text is not log-safe: %q", err)
			}
		})
	}
}

func TestValidateWGKey_Table(t *testing.T) {
	good := testKey(0x11)
	tests := []struct {
		name string
		key  string
		ok   bool
	}{
		{"canonical", good, true},
		{"all zero bytes", testKey(0), true},
		{"empty", "", false},
		{"too short", "AAAA", false},
		{"31 bytes", base64.StdEncoding.EncodeToString(make([]byte, 31)), false},
		{"33 bytes", base64.StdEncoding.EncodeToString(make([]byte, 33)), false},
		{"not base64", strings.Repeat("!", 44), false},
		{"free-form string", "peerPubKeyABC", false},
		{"unpadded", base64.RawStdEncoding.EncodeToString(make([]byte, 32)), false},
		{"url-safe alphabet", base64.URLEncoding.EncodeToString(bytes.Repeat([]byte{0xfb}, 32)), false},
		{"trailing newline", good + "\n", false},
		{"embedded newline", good[:20] + "\n" + good[20:], false},
		{"embedded CRLF", good[:20] + "\r\n" + good[20:], false},
		{"leading space", " " + good, false},
		{"non-zero trailing bits", strings.Repeat("A", 42) + "B=", false},
		{"300 chars", strings.Repeat("A", 300), false},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			err := validateWGKey(tt.key)
			if tt.ok && err != nil {
				t.Fatalf("validateWGKey rejected a good key: %v", err)
			}
			if !tt.ok && err == nil {
				t.Fatalf("validateWGKey accepted %q", tt.key)
			}
		})
	}
}

func TestMeshRange_Env(t *testing.T) {
	tests := []struct {
		name string
		env  string
		want string
	}{
		{"unset uses the pool", "", "100.64.0.0/10"},
		{"override", "10.42.0.0/16", "10.42.0.0/16"},
		{"override is trimmed", "  10.42.0.0/16 \n", "10.42.0.0/16"},
		{"host bits are masked like the engine", "10.42.7.9/16", "10.42.0.0/16"},
		{"garbage falls back to the pool", "banana", "100.64.0.0/10"},
		{"bare address falls back to the pool", "10.42.0.0", "100.64.0.0/10"},
		{"bad prefix length falls back to the pool", "10.42.0.0/99", "100.64.0.0/10"},
		{"ipv6 CIDR is honoured like the engine (no IPv4 peer fits it)", "fd00::/8", "fd00::/8"},
	}
	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			t.Setenv("POTLUCK_MESH_CIDR", tt.env)
			captureLogs(t)
			if got := meshRange(); got.String() != tt.want {
				t.Fatalf("meshRange() = %s, want %s", got, tt.want)
			}
			if got := New().mesh; got.String() != tt.want {
				t.Fatalf("New().mesh = %s, want %s", got, tt.want)
			}
		})
	}
}

func TestLastAddr(t *testing.T) {
	for cidr, want := range map[string]string{
		"100.64.0.0/10": "100.127.255.255",
		"10.42.0.0/16":  "10.42.255.255",
		"100.64.7.0/24": "100.64.7.255",
		"0.0.0.0/0":     "255.255.255.255",
		"10.0.0.5/32":   "10.0.0.5",
	} {
		if got := lastAddr(netip.MustParsePrefix(cidr)); got.String() != want {
			t.Errorf("lastAddr(%s) = %s, want %s", cidr, got, want)
		}
	}
}

func TestReset_MixedRoster(t *testing.T) {
	logs := captureLogs(t)
	s := New()
	bad := peer("bad-gateway", "192.168.1.1", 3)
	roster := []signal.RosterPeer{
		peer("linux", "100.64.0.7", 1),
		bad,
		peer("windows", "100.64.0.8", 2),
		peer("self-twin", "100.64.0.6", 4), // this node's own address
		peer("dup-a", "100.64.0.9", 5),
		peer("dup-b", "100.64.0.9", 6),
		{NodeID: "bad-key", MeshIP: "100.64.0.10", WGPublicKey: "peerPubKeyABC"},
		{NodeID: "hostile", MeshIP: "100.64.0.11\nallowed_ip=0.0.0.0/0", WGPublicKey: testKey(7)},
	}

	kept, rejected := s.Reset("100.64.0.6", roster)

	if want := []string{"linux", "windows"}; !eq(ids(kept), want) {
		t.Fatalf("kept = %v, want %v (roster order)", ids(kept), want)
	}
	if want := []string{"bad-gateway", "bad-key", "dup-a", "dup-b", "hostile", "self-twin"}; !eq(rejectedIDs(rejected), want) {
		t.Fatalf("rejected = %v, want %v", rejectedIDs(rejected), want)
	}
	if want := []string{"linux", "windows"}; !eq(storedIDs(s), want) {
		t.Fatalf("store = %v, want %v", storedIDs(s), want)
	}
	for _, id := range []string{"bad-gateway", "self-twin", "dup-a", "dup-b", "bad-key", "hostile"} {
		if _, ok := s.Get(id); ok {
			t.Errorf("rejected entry %q is in the store", id)
		}
	}

	// One WARN per dropped entry, carrying node_id and a reason, and nothing
	// from the wire that could forge a log line or reveal a key.
	out := logs.String()
	if n := strings.Count(out, "level=WARN"); n != len(rejected) {
		t.Fatalf("got %d WARN lines for %d rejections:\n%s", n, len(rejected), out)
	}
	for _, r := range rejected {
		if !strings.Contains(out, "node_id="+r.NodeID) || !strings.Contains(out, "reason=") {
			t.Errorf("no WARN line for %q:\n%s", r.NodeID, out)
		}
	}
	if strings.Contains(out, "allowed_ip") || strings.Contains(out, testKey(7)) || strings.Contains(out, "peerPubKeyABC") {
		t.Fatalf("log line echoes wire data:\n%s", out)
	}
}

func TestReset_ValidRosterIsStoredAsIs(t *testing.T) {
	logs := captureLogs(t)
	s := New()
	roster := []signal.RosterPeer{peer("a", "100.64.0.7", 1), peer("b", "100.64.0.8", 2), peer("c", "100.64.5.1", 3)}
	kept, rejected := s.Reset("100.64.0.6", roster)
	if len(rejected) != 0 || !eq(ids(kept), []string{"a", "b", "c"}) {
		t.Fatalf("kept=%v rejected=%v", ids(kept), rejected)
	}
	for i, p := range kept {
		if p.MeshIP != roster[i].MeshIP || p.WGPublicKey != roster[i].WGPublicKey {
			t.Errorf("entry %d changed: %+v", i, p)
		}
	}
	if logs.Len() != 0 {
		t.Fatalf("a valid roster logged: %s", logs.String())
	}
}

func TestReset_DuplicateAddressKeepsNeither(t *testing.T) {
	// Every ordering of the claimants gives the same answer.
	a, b, c := peer("a", "100.64.0.9", 1), peer("b", "100.64.0.9", 2), peer("c", "100.64.0.9", 3)
	other := peer("other", "100.64.0.20", 4)
	orders := [][]signal.RosterPeer{
		{a, b, other}, {b, a, other}, {other, a, b}, {a, other, b},
		{a, b, c, other}, {c, b, a, other}, {b, other, c, a},
	}
	for i, order := range orders {
		s := New()
		captureLogs(t)
		kept, _ := s.Reset("100.64.0.6", order)
		if !eq(ids(kept), []string{"other"}) {
			t.Errorf("order %d: kept %v, want only [other]", i, ids(kept))
		}
		if !eq(storedIDs(s), []string{"other"}) {
			t.Errorf("order %d: store %v, want only [other]", i, storedIDs(s))
		}
	}
}

func TestReset_SameNodeListedTwiceIsNotADuplicate(t *testing.T) {
	s := New()
	captureLogs(t)
	p := peer("a", "100.64.0.9", 1)
	kept, rejected := s.Reset("", []signal.RosterPeer{p, p})
	if len(rejected) != 0 || len(kept) != 2 || len(s.All()) != 1 {
		t.Fatalf("kept=%d rejected=%v stored=%d", len(kept), rejected, len(s.All()))
	}
}

func TestReset_ReplacesPreviousContents(t *testing.T) {
	s := New()
	captureLogs(t)
	s.Reset("100.64.0.6", []signal.RosterPeer{peer("old", "100.64.0.7", 1)})
	s.Reset("100.64.0.6", []signal.RosterPeer{peer("new", "100.64.0.8", 2)})
	if !eq(storedIDs(s), []string{"new"}) {
		t.Fatalf("store = %v", storedIDs(s))
	}
}

func TestReset_PeerThatBecomesInvalidIsGone(t *testing.T) {
	s := New()
	captureLogs(t)
	s.Reset("100.64.0.6", []signal.RosterPeer{peer("a", "100.64.0.7", 1), peer("b", "100.64.0.8", 2)})
	kept, rejected := s.Reset("100.64.0.6", []signal.RosterPeer{peer("a", "192.168.1.1", 1), peer("b", "100.64.0.8", 2)})
	if !eq(ids(kept), []string{"b"}) || !eq(rejectedIDs(rejected), []string{"a"}) {
		t.Fatalf("kept=%v rejected=%v", ids(kept), rejectedIDs(rejected))
	}
	if _, ok := s.Get("a"); ok {
		t.Fatal("a is still stored after its entry became invalid")
	}
}

func TestReset_RemembersOwnAddressForMerge(t *testing.T) {
	s := New()
	captureLogs(t)
	s.Reset("100.64.0.6", nil)
	added, _, _, rejected := s.Merge([]signal.RosterPeer{peer("twin", "100.64.0.6", 1)}, nil)
	if len(added) != 0 || !eq(rejectedIDs(rejected), []string{"twin"}) {
		t.Fatalf("added=%v rejected=%v", ids(added), rejectedIDs(rejected))
	}
	if !strings.Contains(rejected[0].Reason, "own address") {
		t.Fatalf("reason = %q", rejected[0].Reason)
	}
}

func TestMerge_ValidUpdateIsStoredAndSplit(t *testing.T) {
	logs := captureLogs(t)
	s := New()
	s.Reset("100.64.0.6", []signal.RosterPeer{peer("a", "100.64.0.7", 1)})
	newcomer := peer("n", "100.64.0.8", 2)
	moved := peer("a", "100.64.0.7", 1)
	ep := "203.0.113.9:51820"
	moved.LastEndpoint = &ep

	added, updated, evict, rejected := s.Merge([]signal.RosterPeer{newcomer}, []signal.RosterPeer{moved})

	if !eq(ids(added), []string{"n"}) || !eq(ids(updated), []string{"a"}) || len(evict) != 0 || len(rejected) != 0 {
		t.Fatalf("added=%v updated=%v evict=%v rejected=%v", ids(added), ids(updated), evict, rejected)
	}
	if got, _ := s.Get("a"); got.LastEndpoint == nil || *got.LastEndpoint != ep {
		t.Fatal("updated endpoint was not stored")
	}
	if !eq(storedIDs(s), []string{"a", "n"}) {
		t.Fatalf("store = %v", storedIDs(s))
	}
	if logs.Len() != 0 {
		t.Fatalf("a valid update logged: %s", logs.String())
	}
}

func TestMerge_RejoinWithSameAddressIsNotADuplicate(t *testing.T) {
	s := New()
	captureLogs(t)
	s.Reset("100.64.0.6", []signal.RosterPeer{peer("a", "100.64.0.7", 1)})
	added, _, evict, rejected := s.Merge([]signal.RosterPeer{peer("a", "100.64.0.7", 1)}, nil)
	if !eq(ids(added), []string{"a"}) || len(evict) != 0 || len(rejected) != 0 {
		t.Fatalf("added=%v evict=%v rejected=%v", ids(added), evict, rejected)
	}
}

func TestMerge_UpdateThatBecomesInvalidEvictsTheStoredPeer(t *testing.T) {
	cases := map[string]signal.RosterPeer{
		"gateway address": peer("a", "192.168.1.1", 1),
		"prefix":          peer("a", "100.64.0.7/32", 1),
		"own address":     peer("a", "100.64.0.6", 1),
		"bad key":         {NodeID: "a", MeshIP: "100.64.0.7", WGPublicKey: "nope"},
	}
	for name, bad := range cases {
		t.Run(name, func(t *testing.T) {
			s := New()
			captureLogs(t)
			s.Reset("100.64.0.6", []signal.RosterPeer{peer("a", "100.64.0.7", 1), peer("b", "100.64.0.8", 2)})
			// Once as an updated entry, once as an added (rejoin) entry.
			for _, viaAdded := range []bool{false, true} {
				var added, updated []signal.RosterPeer
				if viaAdded {
					added = []signal.RosterPeer{bad}
				} else {
					updated = []signal.RosterPeer{bad}
				}
				s.Reset("100.64.0.6", []signal.RosterPeer{peer("a", "100.64.0.7", 1), peer("b", "100.64.0.8", 2)})
				ka, ku, evict, rejected := s.Merge(added, updated)
				if len(ka) != 0 || len(ku) != 0 {
					t.Fatalf("viaAdded=%v: kept %v %v", viaAdded, ids(ka), ids(ku))
				}
				if !eq(evict, []string{"a"}) || !eq(rejectedIDs(rejected), []string{"a"}) {
					t.Fatalf("viaAdded=%v: evict=%v rejected=%v", viaAdded, evict, rejectedIDs(rejected))
				}
				// The caller still gets the old entry from Remove, to tear the peer down.
				old, ok := s.Remove("a")
				if !ok || old.MeshIP != "100.64.0.7" || old.WGPublicKey != testKey(1) {
					t.Fatalf("viaAdded=%v: Remove(a) = %+v, %v", viaAdded, old, ok)
				}
				if _, ok := s.Get("b"); !ok {
					t.Fatal("an unrelated peer was touched")
				}
			}
		})
	}
}

func TestMerge_ClaimOnStoredAddressDropsBoth(t *testing.T) {
	logs := captureLogs(t)
	s := New()
	s.Reset("100.64.0.6", []signal.RosterPeer{peer("a", "100.64.0.7", 1), peer("b", "100.64.0.8", 2)})

	added, _, evict, rejected := s.Merge([]signal.RosterPeer{peer("thief", "100.64.0.7", 9)}, nil)

	if len(added) != 0 || !eq(rejectedIDs(rejected), []string{"thief"}) {
		t.Fatalf("added=%v rejected=%v", ids(added), rejectedIDs(rejected))
	}
	if !eq(evict, []string{"a"}) {
		t.Fatalf("evict = %v, want the stored holder of the address", evict)
	}
	if _, ok := s.Get("thief"); ok {
		t.Fatal("the late claimant was stored")
	}
	if _, ok := s.Get("b"); !ok {
		t.Fatal("an unrelated peer was touched")
	}
	if !strings.Contains(logs.String(), "claimed by more than one node") {
		t.Fatalf("no duplicate warning:\n%s", logs.String())
	}
}

func TestMerge_DuplicatesWithinOneUpdate(t *testing.T) {
	// The claimants may arrive in any order and in any mix of added/updated.
	mk := func(id string, seed byte) signal.RosterPeer { return peer(id, "100.64.0.9", seed) }
	cases := map[string][2][]signal.RosterPeer{
		"two added":          {{mk("x", 1), mk("y", 2)}, nil},
		"two added reversed": {{mk("y", 2), mk("x", 1)}, nil},
		"added and updated":  {{mk("x", 1)}, {mk("y", 2)}},
		"three claimants":    {{mk("x", 1), mk("y", 2), mk("z", 3)}, nil},
		"three reversed":     {{mk("z", 3), mk("y", 2), mk("x", 1)}, nil},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			s := New()
			captureLogs(t)
			s.Reset("100.64.0.6", []signal.RosterPeer{peer("keep", "100.64.0.20", 4)})
			ka, ku, evict, rejected := s.Merge(c[0], c[1])
			if len(ka)+len(ku) != 0 || len(evict) != 0 {
				t.Fatalf("kept %v %v, evict %v", ids(ka), ids(ku), evict)
			}
			if len(rejected) != len(c[0])+len(c[1]) {
				t.Fatalf("rejected %v", rejectedIDs(rejected))
			}
			if !eq(storedIDs(s), []string{"keep"}) {
				t.Fatalf("store = %v", storedIDs(s))
			}
		})
	}
}

func TestMerge_DuplicateInRosterAndLaterUpdateConverge(t *testing.T) {
	// A fresh welcome with the two claimants stores neither. If the reconcile
	// loop later presents both as added in one update, they are refused again, so
	// the node ends up the same as one that stayed connected throughout.
	s := New()
	captureLogs(t)
	a, b := peer("a", "100.64.0.9", 1), peer("b", "100.64.0.9", 2)
	s.Reset("100.64.0.6", []signal.RosterPeer{a, b})
	ka, _, evict, _ := s.Merge([]signal.RosterPeer{b, a}, nil)
	if len(ka) != 0 || len(evict) != 0 || len(s.All()) != 0 {
		t.Fatalf("kept=%v evict=%v stored=%v", ids(ka), evict, storedIDs(s))
	}
}

func TestUpsert(t *testing.T) {
	captureLogs(t)
	s := New()
	s.Reset("100.64.0.6", nil)
	if err := s.Upsert(peer("a", "100.64.0.7", 1)); err != nil {
		t.Fatalf("valid Upsert: %v", err)
	}
	if err := s.Upsert(peer("b", "192.168.1.1", 2)); err == nil {
		t.Fatal("Upsert accepted a gateway address")
	}
	if _, ok := s.Get("b"); ok {
		t.Fatal("rejected entry stored")
	}
	// A bad update for a stored peer removes it, since Upsert has no tunnel to unwind.
	if err := s.Upsert(peer("a", "8.8.8.8", 1)); err == nil {
		t.Fatal("Upsert accepted a public address")
	}
	if _, ok := s.Get("a"); ok {
		t.Fatal("stale entry left behind after an invalid update")
	}
	// A claim on a stored address drops both sides.
	s.Upsert(peer("c", "100.64.0.9", 3))
	if err := s.Upsert(peer("d", "100.64.0.9", 4)); err == nil {
		t.Fatal("Upsert accepted a shared address")
	}
	if len(s.All()) != 0 {
		t.Fatalf("store = %v", storedIDs(s))
	}
}

func TestStoreIsRaceFree(t *testing.T) {
	captureLogs(t)
	s := New()
	var wg sync.WaitGroup
	for g := 0; g < 8; g++ {
		wg.Add(1)
		go func(g int) {
			defer wg.Done()
			for i := 0; i < 200; i++ {
				id := fmt.Sprintf("n%d-%d", g, i%5)
				switch i % 5 {
				case 0:
					s.Reset("100.64.0.6", []signal.RosterPeer{peer(id, fmt.Sprintf("100.64.%d.%d", g, i%200+1), byte(g+1))})
				case 1:
					s.Merge([]signal.RosterPeer{peer(id, fmt.Sprintf("100.64.%d.%d", g, i%200+1), byte(g+1))}, nil)
				case 2:
					s.Upsert(peer(id, "192.168.1.1", 1))
				case 3:
					s.Get(id)
					s.All()
				default:
					s.Remove(id)
				}
			}
		}(g)
	}
	wg.Wait()
}

// ---- household address set (read by the TUN packet filter without a lock) ----

func a4(s string) [4]byte { return netip.MustParseAddr(s).As4() }

func wantIn(t *testing.T, what string, s *Store, in, out []string) {
	t.Helper()
	for _, ip := range in {
		if !s.IsHousehold(a4(ip)) {
			t.Errorf("%s: %s should be in the household set", what, ip)
		}
	}
	for _, ip := range out {
		if s.IsHousehold(a4(ip)) {
			t.Errorf("%s: %s should not be in the household set", what, ip)
		}
	}
}

func TestHouseholdSet_FollowsTheStore(t *testing.T) {
	s := New()
	wantIn(t, "empty store", s, nil, []string{"100.64.0.2", "100.64.0.3"})

	s.Reset("100.64.0.1", []signal.RosterPeer{peer("a", "100.64.0.2", 1), peer("b", "100.64.0.3", 2)})
	wantIn(t, "after Reset", s, []string{"100.64.0.2", "100.64.0.3"}, []string{"100.64.0.1", "100.64.0.4"})

	s.Merge([]signal.RosterPeer{peer("c", "100.64.0.4", 3)}, nil)
	wantIn(t, "after Merge add", s, []string{"100.64.0.2", "100.64.0.3", "100.64.0.4"}, nil)

	s.Remove("b")
	wantIn(t, "after Remove", s, []string{"100.64.0.2", "100.64.0.4"}, []string{"100.64.0.3"})

	if _, ok := s.Remove("b"); ok {
		t.Fatal("second Remove found the peer")
	}
	wantIn(t, "after a Remove of an unknown id", s, []string{"100.64.0.2", "100.64.0.4"}, []string{"100.64.0.3"})

	if err := s.Upsert(peer("d", "100.64.0.9", 4)); err != nil {
		t.Fatal(err)
	}
	wantIn(t, "after Upsert", s, []string{"100.64.0.9"}, nil)

	// A welcome replaces the whole set.
	s.Reset("100.64.0.1", []signal.RosterPeer{peer("a", "100.64.0.2", 1)})
	wantIn(t, "after a second Reset", s, []string{"100.64.0.2"}, []string{"100.64.0.4", "100.64.0.9"})
	s.Reset("100.64.0.1", nil)
	wantIn(t, "after an empty Reset", s, nil, []string{"100.64.0.2"})
}

// Entries the store refuses never reach the set, and a refused update of a held
// peer takes it out only once the store itself drops it.
func TestHouseholdSet_RefusedAndEvictedEntries(t *testing.T) {
	s := New()
	bad := peer("bad", "192.168.1.1", 9)
	s.Reset("100.64.0.1", []signal.RosterPeer{peer("a", "100.64.0.2", 1), bad})
	wantIn(t, "refused at Reset", s, []string{"100.64.0.2"}, []string{"192.168.1.1"})

	// A second node claims a's address: both are refused, a is evicted. a stays
	// in the store (and so in the set) until the caller removes it, exactly as
	// the store reports it.
	_, _, evict, _ := s.Merge([]signal.RosterPeer{peer("thief", "100.64.0.2", 5)}, nil)
	if len(evict) != 1 || evict[0] != "a" {
		t.Fatalf("evict = %v", evict)
	}
	wantIn(t, "evicted but not yet removed", s, []string{"100.64.0.2"}, nil)
	s.Remove("a")
	wantIn(t, "evicted and removed", s, nil, []string{"100.64.0.2"})

	// A held peer whose update is refused is evicted the same way.
	s.Merge([]signal.RosterPeer{peer("b", "100.64.0.3", 2)}, nil)
	_, _, evict, _ = s.Merge(nil, []signal.RosterPeer{peer("b", "10.0.0.5", 2)})
	if len(evict) != 1 || evict[0] != "b" {
		t.Fatalf("evict = %v", evict)
	}
	wantIn(t, "refused update, before removal", s, []string{"100.64.0.3"}, []string{"10.0.0.5"})
	s.Remove("b")
	wantIn(t, "refused update, after removal", s, nil, []string{"100.64.0.3", "10.0.0.5"})
}

// A node that rejoins with the same key and address is a Merge of an entry the
// store already holds: its address stays in the set the whole time.
func TestHouseholdSet_SameKeyRejoinKeepsTheAddress(t *testing.T) {
	s := New()
	s.Reset("100.64.0.1", []signal.RosterPeer{peer("a", "100.64.0.2", 1)})
	before := s.members.Load()
	s.Merge([]signal.RosterPeer{peer("a", "100.64.0.2", 1)}, nil)
	wantIn(t, "after a rejoin Merge", s, []string{"100.64.0.2"}, nil)
	if before == s.members.Load() {
		t.Error("a Merge should publish a fresh snapshot, never edit the one readers hold")
	}
}

// A snapshot a reader already holds never changes.
func TestHouseholdSet_SnapshotsAreImmutable(t *testing.T) {
	s := New()
	s.Reset("100.64.0.1", []signal.RosterPeer{peer("a", "100.64.0.2", 1)})
	old := s.members.Load()
	s.Merge([]signal.RosterPeer{peer("b", "100.64.0.3", 2)}, nil)
	s.Remove("a")
	if !old.Contains(a4("100.64.0.2")) || old.Contains(a4("100.64.0.3")) || old.Len() != 1 {
		t.Error("an earlier snapshot changed after later store calls")
	}
}

func TestAddrSet_NilIsEmpty(t *testing.T) {
	var s *AddrSet
	if s.Contains([4]byte{1, 2, 3, 4}) || s.Len() != 0 {
		t.Error("a nil AddrSet should be empty")
	}
}

// Readers on the packet path run while the store changes. Run with -race.
func TestHouseholdSet_ConcurrentReadersAndWriters(t *testing.T) {
	s := New()
	s.Reset("100.64.0.1", []signal.RosterPeer{peer("stable", "100.64.0.2", 1)})
	var wg sync.WaitGroup
	stop := make(chan struct{})
	for i := 0; i < 4; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			for {
				select {
				case <-stop:
					return
				default:
				}
				if !s.IsHousehold(a4("100.64.0.2")) {
					t.Error("a peer that was never removed went missing from the set")
					return
				}
				s.IsHousehold(a4("100.64.0.3"))
			}
		}()
	}
	for i := 0; i < 2000; i++ {
		s.Merge([]signal.RosterPeer{peer("x", "100.64.0.3", 2)}, nil)
		s.Remove("x")
		if i%50 == 0 {
			s.Reset("100.64.0.1", []signal.RosterPeer{peer("stable", "100.64.0.2", 1)})
		}
	}
	close(stop)
	wg.Wait()
}
