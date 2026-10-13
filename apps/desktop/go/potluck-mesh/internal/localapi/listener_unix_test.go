//go:build !windows

package localapi

import "testing"

// ---------------------------------------------------------------------------
// decideSocketOwner
// ---------------------------------------------------------------------------

func TestDecideSocketOwner_NonRoot_NeverChowns(t *testing.T) {
	resolverCalled := false
	resolver := func() (int, int, bool) {
		resolverCalled = true
		return 501, 20, true
	}
	uid, gid, source, ok := decideSocketOwner(501, "", "", resolver)
	if ok {
		t.Fatalf("expected shouldChown=false for non-root euid, got uid=%d gid=%d source=%q", uid, gid, source)
	}
	if resolverCalled {
		t.Error("resolver should never be consulted when euid != 0")
	}
}

func TestDecideSocketOwner_SudoUID_NoGID_DefaultsGIDToUID(t *testing.T) {
	uid, gid, source, ok := decideSocketOwner(0, "501", "", nil)
	if !ok {
		t.Fatal("expected shouldChown=true")
	}
	if uid != 501 || gid != 501 {
		t.Errorf("uid/gid = %d/%d, want 501/501 (gid defaults to uid when SUDO_GID unset)", uid, gid)
	}
	if source != "sudo_uid" {
		t.Errorf("source = %q, want %q", source, "sudo_uid")
	}
}

func TestDecideSocketOwner_SudoUIDAndGID(t *testing.T) {
	uid, gid, source, ok := decideSocketOwner(0, "501", "20", nil)
	if !ok {
		t.Fatal("expected shouldChown=true")
	}
	if uid != 501 || gid != 20 {
		t.Errorf("uid/gid = %d/%d, want 501/20", uid, gid)
	}
	if source != "sudo_uid" {
		t.Errorf("source = %q, want %q", source, "sudo_uid")
	}
}

func TestDecideSocketOwner_SudoUID_UnparseableFallsThroughToResolver(t *testing.T) {
	resolver := func() (int, int, bool) { return 1000, 1000, true }
	uid, gid, source, ok := decideSocketOwner(0, "not-a-number", "", resolver)
	if !ok {
		t.Fatal("expected shouldChown=true via resolver fallback")
	}
	if uid != 1000 || gid != 1000 {
		t.Errorf("uid/gid = %d/%d, want 1000/1000", uid, gid)
	}
	if source != "console_user" {
		t.Errorf("source = %q, want %q", source, "console_user")
	}
}

// TestDecideSocketOwner_RootNoSudoUID_UsesConsoleResolver is the core test
// for the root-service case: euid==0 (a root LaunchDaemon/systemd
// service), no SUDO_UID (launchd/systemd never sets it - only a `sudo`
// invocation does), and the console-user resolver finds a logged-in user.
// The socket must be chown'd to that user rather than left root-owned.
func TestDecideSocketOwner_RootNoSudoUID_UsesConsoleResolver(t *testing.T) {
	resolver := func() (int, int, bool) { return 501, 20, true }
	uid, gid, source, ok := decideSocketOwner(0, "", "", resolver)
	if !ok {
		t.Fatal("expected shouldChown=true from the console-user resolver")
	}
	if uid != 501 || gid != 20 {
		t.Errorf("uid/gid = %d/%d, want 501/20", uid, gid)
	}
	if source != "console_user" {
		t.Errorf("source = %q, want %q", source, "console_user")
	}
}

func TestDecideSocketOwner_RootNoSudoUID_ResolverNotOK_LeavesRootOwned(t *testing.T) {
	resolver := func() (int, int, bool) { return 0, 0, false }
	_, _, _, ok := decideSocketOwner(0, "", "", resolver)
	if ok {
		t.Error("expected shouldChown=false when the resolver cannot determine a console user (e.g. headless server)")
	}
}

func TestDecideSocketOwner_RootNoSudoUID_NilResolver_LeavesRootOwned(t *testing.T) {
	_, _, _, ok := decideSocketOwner(0, "", "", nil)
	if ok {
		t.Error("expected shouldChown=false with a nil resolver")
	}
}

// ---------------------------------------------------------------------------
// consoleUserResolver injection (package-level var swap)
// ---------------------------------------------------------------------------

func TestConsoleUserResolver_IsInjectable(t *testing.T) {
	orig := consoleUserResolver
	t.Cleanup(func() { consoleUserResolver = orig })

	consoleUserResolver = func() (int, int, bool) { return 42, 42, true }
	uid, gid, ok := consoleUserResolver()
	if !ok || uid != 42 || gid != 42 {
		t.Fatalf("injected resolver not honoured: uid=%d gid=%d ok=%v", uid, gid, ok)
	}
}

// ---------------------------------------------------------------------------
// parseStatConsoleOutput (macOS)
// ---------------------------------------------------------------------------

func TestParseStatConsoleOutput(t *testing.T) {
	cases := []struct {
		name     string
		in       string
		wantUser string
		wantOK   bool
	}{
		{"normal user", "sam\n", "sam", true},
		{"trims whitespace", "  sam  \n", "sam", true},
		{"root means nobody logged in", "root\n", "", false},
		{"empty output", "", "", false},
		{"whitespace only", "   \n", "", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			user, ok := parseStatConsoleOutput(tc.in)
			if ok != tc.wantOK || user != tc.wantUser {
				t.Errorf("parseStatConsoleOutput(%q) = (%q, %v), want (%q, %v)", tc.in, user, ok, tc.wantUser, tc.wantOK)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// parseLoginctlSessions (Linux)
// ---------------------------------------------------------------------------

func TestParseLoginctlSessions(t *testing.T) {
	cases := []struct {
		name     string
		in       string
		wantUser string
		wantOK   bool
	}{
		{
			name:     "single seat0 session",
			in:       "        1 1000 sam      seat0\n",
			wantUser: "sam",
			wantOK:   true,
		},
		{
			name: "prefers seat0 over a non-seat0 session",
			in: "        1 1000 alice\n" +
				"        2 1001 bob      seat0\n",
			wantUser: "bob",
			wantOK:   true,
		},
		{
			name:     "no seat column falls back to first session",
			in:       "        1 1000 alice\n",
			wantUser: "alice",
			wantOK:   true,
		},
		{
			name:     "empty output",
			in:       "",
			wantUser: "",
			wantOK:   false,
		},
		{
			name:     "malformed line skipped",
			in:       "garbage\n        1 1000 sam      seat0\n",
			wantUser: "sam",
			wantOK:   true,
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			user, ok := parseLoginctlSessions(tc.in)
			if ok != tc.wantOK || user != tc.wantUser {
				t.Errorf("parseLoginctlSessions(%q) = (%q, %v), want (%q, %v)", tc.in, user, ok, tc.wantUser, tc.wantOK)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// parseWhoOutput
// ---------------------------------------------------------------------------

func TestParseWhoOutput(t *testing.T) {
	cases := []struct {
		name     string
		in       string
		wantUser string
		wantOK   bool
	}{
		{"single session", "sam      pts/0        2026-07-17 10:00\n", "sam", true},
		{"multiple sessions returns first", "sam      pts/0        2026-07-17 10:00\nalice    pts/1        2026-07-17 11:00\n", "sam", true},
		{"empty output", "", "", false},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			user, ok := parseWhoOutput(tc.in)
			if ok != tc.wantOK || user != tc.wantUser {
				t.Errorf("parseWhoOutput(%q) = (%q, %v), want (%q, %v)", tc.in, user, ok, tc.wantUser, tc.wantOK)
			}
		})
	}
}
