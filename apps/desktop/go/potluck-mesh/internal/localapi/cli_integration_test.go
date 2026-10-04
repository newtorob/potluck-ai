//go:build !windows

package localapi_test

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/potluck-ai/potluck-mesh/internal/coordapi"
	"github.com/potluck-ai/potluck-mesh/internal/localapi"
)

// Run with POTLUCK_CLI_ENTRY=/absolute/path/apps/cli/dist/index.js after building
// the CLI. Exercises the real Go IPC handlers, without requiring a TUN or root.
func TestCLIPairUnpair(t *testing.T) {
	entry := os.Getenv("POTLUCK_CLI_ENTRY")
	if entry == "" {
		t.Skip("opt-in built CLI contract smoke")
	}
	requests := make(chan map[string]any, 1)
	coordinator := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.URL.Path != "/mesh/enroll" {
			http.NotFound(w, r)
			return
		}
		var body map[string]any
		if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
			http.Error(w, "bad body", 400)
			return
		}
		requests <- body
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		fmt.Fprint(w, `{"node_id":"11111111-1111-1111-1111-111111111111","household_id":"22222222-2222-2222-2222-222222222222","mesh_ip":"100.64.0.7","mesh_cidr":"100.64.0.0/24","peers":[],"node_token":"private-node-credential"}`)
	}))
	defer coordinator.Close()
	cfg := defaultCfg(t)
	cfg.State.SetCoordinatorURL(coordinator.URL)
	cfg.CoordClient = coordapi.New(coordinator.URL)
	cfg.SigningKeyPath = filepath.Join(t.TempDir(), "signing.key")
	cfg.NodeTokenPath = filepath.Join(t.TempDir(), "node.token")
	cfg.SocketPath = fmt.Sprintf("/tmp/potluck-cli-%d.sock", time.Now().UnixNano())
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	srv := localapi.New(cfg)
	done := make(chan error, 1)
	go func() { done <- srv.Serve(ctx) }()
	t.Cleanup(func() {
		cancel()
		select {
		case <-done:
		case <-time.After(5 * time.Second):
			t.Error("IPC server did not stop")
		}
		os.Remove(cfg.SocketPath)
	})
	deadline := time.Now().Add(5 * time.Second)
	for {
		if _, err := os.Stat(cfg.SocketPath); err == nil {
			break
		}
		if time.Now().After(deadline) {
			t.Fatal("IPC socket did not appear")
		}
		time.Sleep(10 * time.Millisecond)
	}
	invitation := filepath.Join(t.TempDir(), "invite.json")
	data, _ := json.Marshal(map[string]any{"schemaVersion": 1, "token": "private-pairing-credential", "householdId": "22222222-2222-2222-2222-222222222222", "coordinatorUrl": coordinator.URL, "expiresAt": time.Now().Add(time.Minute).Format(time.RFC3339)})
	if err := os.WriteFile(invitation, data, 0600); err != nil {
		t.Fatal(err)
	}
	stateDir := t.TempDir()
	run := func(args ...string) map[string]any {
		t.Helper()
		commandCtx, stop := context.WithTimeout(context.Background(), 10*time.Second)
		defer stop()
		cmd := exec.CommandContext(commandCtx, "node", append([]string{entry}, args...)...)
		cmd.Env = append(os.Environ(), "POTLUCK_DATA_DIR="+stateDir, "POTLUCK_LOCAL_API_SOCKET="+cfg.SocketPath, "NO_COLOR=1")
		output, err := cmd.CombinedOutput()
		if err != nil {
			t.Fatalf("CLI %s failed: %v %s", args[0], err, output)
		}
		if strings.Contains(string(output), "private-") {
			t.Fatal("CLI exposed a credential")
		}
		var report map[string]any
		if err := json.Unmarshal(output, &report); err != nil {
			t.Fatalf("CLI did not return one JSON report: %v", err)
		}
		return report
	}
	if run("connect", "--invite-file", invitation, "--name", "SSH machine", "--json")["state"] != "connected" {
		t.Fatal("machine not connected")
	}
	request := <-requests
	if request["pairing_token"] != "private-pairing-credential" || request["node_name"] != "SSH machine" {
		t.Fatal("wrong enrollment request")
	}
	if _, err := os.Stat(cfg.NodeTokenPath); err != nil {
		t.Fatal("node token not persisted")
	}
	if run("connect", "--invite-file", invitation, "--json")["state"] != "already_connected" {
		t.Fatal("repeated connect was not idempotent")
	}
	if run("disconnect", "--json")["state"] != "disconnected" {
		t.Fatal("machine not disconnected")
	}
	if _, err := os.Stat(cfg.NodeTokenPath); !os.IsNotExist(err) {
		t.Fatal("node token survived disconnect")
	}
}
