package main

import (
	"go/build"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

// The packet filter wiring has one variant for the default build and one that
// only the mesh-live harness compiles (tag meshlive_filter, source under
// scripts/dev/mesh-live/shim). The default build must contain no trace of the
// second one and no runtime way to reach it.

func goFiles(t *testing.T, tags ...string) []string {
	t.Helper()
	ctx := build.Default
	ctx.BuildTags = tags
	pkg, err := ctx.ImportDir(".", 0)
	if err != nil {
		t.Fatal(err)
	}
	files := append([]string(nil), pkg.GoFiles...)
	sort.Strings(files)
	return files
}

func TestDefaultBuildCompilesTheDefaultFilterWiringOnly(t *testing.T) {
	def := goFiles(t)
	has := func(files []string, name string) bool {
		for _, f := range files {
			if f == name {
				return true
			}
		}
		return false
	}
	if !has(def, "packetfilter.go") {
		t.Fatalf("the default build does not compile packetfilter.go: %v", def)
	}
	tagged := goFiles(t, "meshlive_filter")
	if has(tagged, "packetfilter.go") {
		t.Error("the meshlive_filter tag still compiles the default packetfilter.go, so the harness variant would clash")
	}
	// The tag must replace exactly that one file and add nothing else here.
	if len(tagged) != len(def)-1 {
		t.Errorf("the tag changes more than packetfilter.go: default %v, tagged %v", def, tagged)
	}
}

// Nothing outside packetfilter.go may mention the harness variant, and nothing
// in the shipped source may read a setting that restricts or unrestricts a peer.
func TestShippedSourceHasNoHarnessHooks(t *testing.T) {
	var scanned int
	err := filepath.Walk("../..", func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.IsDir() && (info.Name() == "third_party" || info.Name() == "dist") {
			return filepath.SkipDir
		}
		if info.IsDir() || !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
			return nil
		}
		b, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		scanned++
		src := string(b)
		if strings.Contains(src, "meshlive") && filepath.Base(path) != "packetfilter.go" {
			t.Errorf("%s mentions the harness (meshlive)", path)
		}
		for _, bad := range []string{"PORTFILTER", "MESHLIVE_", "RESTRICT_PEER"} {
			if strings.Contains(src, bad) {
				t.Errorf("%s contains %q: a filter setting read from the environment", path, bad)
			}
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if scanned < 20 {
		t.Errorf("scanned only %d source files; the walk is looking in the wrong place", scanned)
	}
}
