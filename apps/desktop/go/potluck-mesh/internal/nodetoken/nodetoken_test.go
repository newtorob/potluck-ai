package nodetoken_test

import (
	"os"
	"path/filepath"
	"runtime"
	"testing"

	"github.com/potluck-ai/potluck-mesh/internal/nodetoken"
)

func TestPath_SiblingOfWGKey(t *testing.T) {
	got := nodetoken.Path("/var/lib/potluck/mesh/wg.key")
	want := "/var/lib/potluck/mesh/node.token"
	if got != want {
		t.Errorf("Path: got %q want %q", got, want)
	}
}

func TestLoad_MissingFile_ReturnsEmptyNoError(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "node.token")

	got, err := nodetoken.Load(path)
	if err != nil {
		t.Fatalf("Load: unexpected error: %v", err)
	}
	if got != "" {
		t.Errorf("Load: got %q, want empty string for a missing file", got)
	}
}

func TestSaveThenLoad_Roundtrips(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "node.token")
	const token = "abcDEF123_-xyzABCdef123_-xyzABCdef123AB" // 43-char b64url-shaped stand-in

	if err := nodetoken.Save(path, token); err != nil {
		t.Fatalf("Save: %v", err)
	}

	got, err := nodetoken.Load(path)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if got != token {
		t.Errorf("Load after Save: got %q want %q", got, token)
	}
}

func TestSave_CreatesParentDirAndFileMode0600(t *testing.T) {
	if runtime.GOOS == "windows" {
		t.Skip("POSIX file-mode semantics don't apply on Windows")
	}
	dir := t.TempDir()
	nested := filepath.Join(dir, "nested", "mesh")
	path := filepath.Join(nested, "node.token")

	if err := nodetoken.Save(path, "tok"); err != nil {
		t.Fatalf("Save: %v", err)
	}

	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("stat: %v", err)
	}
	if perm := info.Mode().Perm(); perm != 0600 {
		t.Errorf("file mode: got %o want %o", perm, 0600)
	}

	dirInfo, err := os.Stat(nested)
	if err != nil {
		t.Fatalf("stat dir: %v", err)
	}
	if perm := dirInfo.Mode().Perm(); perm != 0700 {
		t.Errorf("dir mode: got %o want %o", perm, 0700)
	}
}

func TestSave_OverwritesExisting(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "node.token")

	if err := nodetoken.Save(path, "first-token"); err != nil {
		t.Fatalf("Save (first): %v", err)
	}
	if err := nodetoken.Save(path, "second-token"); err != nil {
		t.Fatalf("Save (second): %v", err)
	}

	got, err := nodetoken.Load(path)
	if err != nil {
		t.Fatalf("Load: %v", err)
	}
	if got != "second-token" {
		t.Errorf("Load after overwrite: got %q want %q", got, "second-token")
	}
}

func TestClear_RemovesFile(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "node.token")

	if err := nodetoken.Save(path, "tok"); err != nil {
		t.Fatalf("Save: %v", err)
	}
	if err := nodetoken.Clear(path); err != nil {
		t.Fatalf("Clear: %v", err)
	}
	if _, err := os.Stat(path); !os.IsNotExist(err) {
		t.Errorf("expected file to be gone, stat err = %v", err)
	}

	// A subsequent Load must report "not enrolled" (empty, no error), not a
	// stale value or a hard failure.
	got, err := nodetoken.Load(path)
	if err != nil {
		t.Fatalf("Load after Clear: unexpected error: %v", err)
	}
	if got != "" {
		t.Errorf("Load after Clear: got %q, want empty", got)
	}
}

func TestClear_MissingFile_IsIdempotent(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "node.token")

	if err := nodetoken.Clear(path); err != nil {
		t.Fatalf("Clear on a never-created file should be a no-op, got: %v", err)
	}
}
