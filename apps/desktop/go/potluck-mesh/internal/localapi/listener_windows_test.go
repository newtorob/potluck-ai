//go:build windows

package localapi

import "testing"

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
