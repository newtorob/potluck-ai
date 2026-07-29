package runtimesettings_test

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/potluck-ai/potluck-mesh/internal/runtimesettings"
)

func TestPath(t *testing.T) {
	got := runtimesettings.Path("/var/lib/potluck/mesh/wg.key")
	want := "/var/lib/potluck/mesh/settings.json"
	if got != want {
		t.Errorf("Path() = %q, want %q", got, want)
	}
}

func TestLoad_MissingFile_ReturnsZeroValueNoError(t *testing.T) {
	path := filepath.Join(t.TempDir(), "settings.json")
	s, err := runtimesettings.Load(path)
	if err != nil {
		t.Fatalf("Load() error = %v, want nil for a missing file", err)
	}
	if s.CoordinatorURL != "" {
		t.Errorf("CoordinatorURL = %q, want empty (no override)", s.CoordinatorURL)
	}
}

func TestSaveThenLoad_RoundTrips(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, "sub", "settings.json")

	want := runtimesettings.Settings{CoordinatorURL: "https://coordinator.example.com"}
	if err := runtimesettings.Save(path, want); err != nil {
		t.Fatalf("Save() error = %v", err)
	}

	got, err := runtimesettings.Load(path)
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if got != want {
		t.Errorf("Load() = %+v, want %+v", got, want)
	}
}

func TestSave_FileModeIs0600(t *testing.T) {
	path := filepath.Join(t.TempDir(), "settings.json")
	if err := runtimesettings.Save(path, runtimesettings.Settings{CoordinatorURL: "https://x.example.com"}); err != nil {
		t.Fatalf("Save() error = %v", err)
	}
	info, err := os.Stat(path)
	if err != nil {
		t.Fatalf("Stat() error = %v", err)
	}
	if perm := info.Mode().Perm(); perm != 0600 {
		t.Errorf("file mode = %o, want 0600", perm)
	}
}

func TestSave_CreatesParentDirAt0700(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "nested", "dir")
	path := filepath.Join(dir, "settings.json")
	if err := runtimesettings.Save(path, runtimesettings.Settings{CoordinatorURL: "https://x.example.com"}); err != nil {
		t.Fatalf("Save() error = %v", err)
	}
	info, err := os.Stat(dir)
	if err != nil {
		t.Fatalf("Stat(dir) error = %v", err)
	}
	if !info.IsDir() {
		t.Fatalf("%s is not a directory", dir)
	}
	if perm := info.Mode().Perm(); perm != 0700 {
		t.Errorf("dir mode = %o, want 0700", perm)
	}
}

func TestSave_Overwrite_ReplacesPreviousValue(t *testing.T) {
	path := filepath.Join(t.TempDir(), "settings.json")
	if err := runtimesettings.Save(path, runtimesettings.Settings{CoordinatorURL: "https://old.example.com"}); err != nil {
		t.Fatalf("Save() error = %v", err)
	}
	if err := runtimesettings.Save(path, runtimesettings.Settings{CoordinatorURL: "https://new.example.com"}); err != nil {
		t.Fatalf("Save() error = %v", err)
	}
	got, err := runtimesettings.Load(path)
	if err != nil {
		t.Fatalf("Load() error = %v", err)
	}
	if got.CoordinatorURL != "https://new.example.com" {
		t.Errorf("CoordinatorURL = %q, want %q", got.CoordinatorURL, "https://new.example.com")
	}
}

func TestLoad_CorruptFile_ReturnsError(t *testing.T) {
	path := filepath.Join(t.TempDir(), "settings.json")
	if err := os.WriteFile(path, []byte("not json"), 0600); err != nil {
		t.Fatalf("WriteFile() error = %v", err)
	}
	if _, err := runtimesettings.Load(path); err == nil {
		t.Error("Load() error = nil, want an error for corrupt JSON")
	}
}
