//go:build windows

package localapi

import (
	"context"
	"fmt"
	"io"
	"net"
	"os"
	"os/user"
	"strings"
	"testing"
	"time"

	"github.com/Microsoft/go-winio"
	"golang.org/x/sys/windows"
)

func TestBuildPipeSDDL_OwnerOnly_WhenConsoleSIDEmpty(t *testing.T) {
	got := buildPipeSDDL("S-1-5-18", "")
	want := "D:P(A;;GA;;;S-1-5-18)"
	if got != want {
		t.Errorf("buildPipeSDDL(owner, \"\") = %q, want %q", got, want)
	}
}

func TestBuildPipeSDDL_OwnerOnly_WhenConsoleSIDMatchesOwner(t *testing.T) {
	got := buildPipeSDDL("S-1-5-21-1-2-3-1001", "S-1-5-21-1-2-3-1001")
	want := "D:P(A;;GA;;;S-1-5-21-1-2-3-1001)"
	if got != want {
		t.Errorf("buildPipeSDDL(same, same) = %q, want %q", got, want)
	}
}

func TestBuildPipeSDDL_GrantsBoth_WhenServiceAndConsoleDiffer(t *testing.T) {
	// The load-bearing case: daemon runs as LocalSystem (S-1-5-18), desktop
	// user is a normal account SID — both must be granted or the app can't
	// reach the pipe.
	got := buildPipeSDDL("S-1-5-18", "S-1-5-21-1-2-3-1001")
	want := "D:P(A;;GA;;;S-1-5-18)(A;;GA;;;S-1-5-21-1-2-3-1001)"
	if got != want {
		t.Errorf("buildPipeSDDL(LocalSystem, user) = %q, want %q", got, want)
	}
}

// The DACL for every console state the follow loop can ask for: nobody,
// the same account as the service, and two different users in turn.
func TestBuildPipeSDDL_ForEachConsoleKey(t *testing.T) {
	const system = "S-1-5-18"
	const userA = "S-1-5-21-1-2-3-1001"
	const userB = "S-1-5-21-1-2-3-1002"
	cases := []struct {
		name, key, want string
	}{
		{"nobody", "", "D:P(A;;GA;;;S-1-5-18)"},
		{"same as service", system, "D:P(A;;GA;;;S-1-5-18)"},
		{"user A", userA, "D:P(A;;GA;;;S-1-5-18)(A;;GA;;;" + userA + ")"},
		{"user B", userB, "D:P(A;;GA;;;S-1-5-18)(A;;GA;;;" + userB + ")"},
	}
	for _, c := range cases {
		if got := buildPipeSDDL(system, c.key); got != c.want {
			t.Errorf("%s: buildPipeSDDL(%q, %q) = %q, want %q", c.name, system, c.key, got, c.want)
		}
		// Each DACL names one console user at most, never a group.
		if n := strings.Count(buildPipeSDDL(system, c.key), "(A;;"); n > 2 {
			t.Errorf("%s: DACL has %d ACEs, want at most 2", c.name, n)
		}
	}
}

// readPipeDACL opens the pipe for reading its security descriptor and returns
// the DACL as SDDL. The dial is also a plain connection, so it shows the
// caller can get in.
func readPipeDACL(name string) (string, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	c, err := winio.DialPipeAccess(ctx, name, windows.GENERIC_READ|windows.READ_CONTROL)
	if err != nil {
		return "", err
	}
	defer c.Close()
	fd, ok := c.(interface{ Fd() uintptr })
	if !ok {
		return "", fmt.Errorf("pipe connection %T has no Fd", c)
	}
	sd, err := windows.GetSecurityInfo(windows.Handle(fd.Fd()), windows.SE_KERNEL_OBJECT, windows.DACL_SECURITY_INFORMATION)
	if err != nil {
		return "", fmt.Errorf("GetSecurityInfo on a client pipe handle: %w", err)
	}
	return sd.String(), nil
}

// grantsSID reports whether an SDDL string has an ACE for sid. SDDL prints
// well-known SIDs by their two-letter alias, so the alias counts too.
func grantsSID(sddl, sid, alias string) bool {
	return strings.Contains(sddl, sid) || strings.Contains(sddl, ";;;"+alias+")")
}

func pipeEcho(c net.Conn, msg string) error {
	_ = c.SetDeadline(time.Now().Add(2 * time.Second))
	if _, err := c.Write([]byte(msg)); err != nil {
		return err
	}
	buf := make([]byte, len(msg))
	if _, err := io.ReadFull(c, buf); err != nil {
		return err
	}
	if string(buf) != msg {
		return fmt.Errorf("echo = %q, want %q", buf, msg)
	}
	return nil
}

// A real named pipe and a real follower, with a stand-in for the console user.
//
// The test account plays the service account and is granted full access, as
// LocalSystem would be: go-winio needs that to create the pipe's later
// instances, and a client open needs it too. The "console user" the test
// grants and revokes is a different well-known SID (Guests, then Backup
// Operators), and the test reads the DACL back to see the in-place change.
// A connection that is open across every change has to keep working.
func TestPipeDACLFollowsConsoleKeyInPlace(t *testing.T) {
	const (
		guests, guestsAlias = "S-1-5-32-546", "BG"
		backup, backupAlias = "S-1-5-32-551", "BO"
	)
	u, err := user.Current()
	if err != nil {
		t.Fatal(err)
	}
	name := fmt.Sprintf(`\\.\pipe\potluck-mesh-test-%d-%d`, os.Getpid(), time.Now().UnixNano())

	ks := &keySource{}
	ln, err := listenPipe(name, u.Uid, ks.get, 20*time.Millisecond)
	if err != nil {
		t.Fatalf("listenPipe: %v", err)
	}
	t.Cleanup(func() { _ = ln.Close() })

	go func() {
		for {
			c, err := ln.Accept()
			if err != nil {
				return
			}
			go func() { defer c.Close(); _, _ = io.Copy(c, c) }()
		}
	}()

	dacl := func() string {
		t.Helper()
		sddl, err := readPipeDACL(name)
		if err != nil {
			t.Fatalf("read the pipe's DACL: %v", err)
		}
		return sddl
	}
	waitFor := func(what string, cond func(sddl string) bool) {
		t.Helper()
		var last string
		eventually(t, func() bool {
			sddl, err := readPipeDACL(name)
			last = sddl
			return err == nil && cond(sddl)
		})
		t.Logf("%s: DACL is now %s", what, last)
	}

	if sddl := dacl(); grantsSID(sddl, guests, guestsAlias) {
		t.Fatalf("with nobody at the console the DACL already grants Guests: %s", sddl)
	}

	// A connection that stays open through every change below.
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	held, err := winio.DialPipeContext(ctx, name)
	if err != nil {
		t.Fatal(err)
	}
	defer held.Close()
	if err := pipeEcho(held, "start"); err != nil {
		t.Fatalf("held connection before any change: %v", err)
	}

	// nobody -> Guests
	ks.set(guests)
	waitFor("nobody to Guests", func(s string) bool { return grantsSID(s, guests, guestsAlias) })
	if err := pipeEcho(held, "after grant"); err != nil {
		t.Fatalf("held connection after the grant: %v", err)
	}

	// Guests -> Backup Operators (one console user replaced by another)
	ks.set(backup)
	waitFor("Guests to Backup Operators", func(s string) bool {
		return grantsSID(s, backup, backupAlias) && !grantsSID(s, guests, guestsAlias)
	})
	if err := pipeEcho(held, "after switch"); err != nil {
		t.Fatalf("held connection after the switch: %v", err)
	}

	// Backup Operators -> nobody
	ks.set("")
	waitFor("Backup Operators to nobody", func(s string) bool {
		return !grantsSID(s, backup, backupAlias) && !grantsSID(s, guests, guestsAlias)
	})
	if err := pipeEcho(held, "after revoke"); err != nil {
		t.Fatalf("held connection after the revoke: %v", err)
	}

	// New connections still work for the service account, and Close stops
	// the follower and the pipe.
	if _, err := readPipeDACL(name); err != nil {
		t.Fatalf("new connection after all changes: %v", err)
	}
	_ = ln.Close()
	late, cancelLate := context.WithTimeout(context.Background(), 500*time.Millisecond)
	defer cancelLate()
	held.Close()
	if c, err := winio.DialPipeContext(late, name); err == nil {
		c.Close()
		t.Error("the pipe still accepts connections after Close")
	}
}
