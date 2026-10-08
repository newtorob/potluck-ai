//go:build !windows

package main

import (
	"context"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"sort"
	"testing"
	"time"

	"github.com/potluck-ai/potluck-mesh/internal/events"
	"github.com/potluck-ai/potluck-mesh/internal/localapi"
	msignal "github.com/potluck-ai/potluck-mesh/internal/signal"
)

// Handler to store to GET /peers, over the real local API socket: whatever the
// engine reads there is only ever a validated entry.
func TestLocalAPIPeers_NeverListsRejectedEntries(t *testing.T) {
	captureSlog(t)
	f := newRosterFixture(t)

	sock := fmt.Sprintf("/tmp/potluck-roster-test-%d.sock", time.Now().UnixNano())
	t.Cleanup(func() { _ = os.Remove(sock) })
	srv := localapi.New(localapi.Config{
		Bus:           events.NewBus(),
		PeerStore:     f.h.ps,
		State:         localapi.NewState(),
		WGPrivKeyPath: t.TempDir() + "/wg.key",
		SocketPath:    sock,
		Platform:      "darwin",
	})
	ctx, cancel := context.WithCancel(context.Background())
	t.Cleanup(cancel)
	go func() { _ = srv.Serve(ctx) }()
	deadline := time.Now().Add(5 * time.Second)
	for {
		if _, err := os.Stat(sock); err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("local API did not start")
		}
		time.Sleep(10 * time.Millisecond)
	}
	client := &http.Client{Transport: &http.Transport{
		DialContext: func(ctx context.Context, _, _ string) (net.Conn, error) {
			return (&net.Dialer{}).DialContext(ctx, "unix", sock)
		},
	}}
	listed := func() []string {
		t.Helper()
		resp, err := client.Get("http://localhost/peers")
		if err != nil {
			t.Fatalf("GET /peers: %v", err)
		}
		defer resp.Body.Close()
		var body struct {
			Peers []struct {
				NodeID string `json:"node_id"`
				MeshIP string `json:"mesh_ip"`
			} `json:"peers"`
		}
		if err := json.NewDecoder(resp.Body).Decode(&body); err != nil {
			t.Fatalf("decode /peers: %v", err)
		}
		var out []string
		for _, p := range body.Peers {
			out = append(out, p.NodeID+"@"+p.MeshIP)
		}
		sort.Strings(out)
		return out
	}

	f.h.OnWelcome(welcome(mixedRoster()...))
	wantStrings(t, "/peers after welcome", listed(), []string{"linux@100.64.0.7", "windows@100.64.0.8"})

	// A listed peer's entry turns invalid.
	f.h.OnRosterUpdate(msignal.RosterUpdate{Updated: []msignal.RosterPeer{rp("linux", "8.8.8.8", 1)}})
	wantStrings(t, "/peers after update", listed(), []string{"windows@100.64.0.8"})

	// A new node claims a listed peer's address: neither is listed.
	f.h.OnRosterUpdate(msignal.RosterUpdate{Added: []msignal.RosterPeer{rp("thief", "100.64.0.8", 9)}})
	wantStrings(t, "/peers after claim", listed(), nil)
}
