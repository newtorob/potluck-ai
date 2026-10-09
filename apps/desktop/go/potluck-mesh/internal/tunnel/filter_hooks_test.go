package tunnel

import (
	"go/ast"
	"go/build"
	"go/parser"
	"go/token"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

// The shipped filter must carry nothing that exists only for a test or for the
// mesh-live harness: no exported setter that weakens it, no package-level
// function variable to swap, no environment switch, no build tag. These tests
// read the source of the default build and fail if any of that appears. Test
// seams (the clock, the household function) are unexported struct fields that
// only this package's own tests can reach.

func parseNonTest(t *testing.T, file string) *ast.File {
	t.Helper()
	f, err := parser.ParseFile(token.NewFileSet(), file, nil, parser.ParseComments)
	if err != nil {
		t.Fatal(err)
	}
	return f
}

func TestFilterPublicSurfaceIsExactlyTheAPI(t *testing.T) {
	want := []string{
		"ErrBadPeer", "ErrBadTTL", "ErrTooManyEntries",
		"EntryInfo", "FilterStats", "Grant", "PacketFilter", "NewPacketFilter",
		"PacketFilter.Entries", "PacketFilter.RemoveEntry", "PacketFilter.SetEntry",
		"PacketFilter.SetSelf", "PacketFilter.Stats", "PacketFilter.Wrap",
		"FilterStats.Total",
	}
	var got []string
	for _, d := range parseNonTest(t, "filter.go").Decls {
		switch d := d.(type) {
		case *ast.FuncDecl:
			if !d.Name.IsExported() {
				continue
			}
			name := d.Name.Name
			if d.Recv != nil && len(d.Recv.List) == 1 {
				rt := d.Recv.List[0].Type
				if star, ok := rt.(*ast.StarExpr); ok {
					rt = star.X
				}
				if id, ok := rt.(*ast.Ident); ok {
					if !id.IsExported() {
						continue
					}
					name = id.Name + "." + name
				}
			}
			got = append(got, name)
		case *ast.GenDecl:
			for _, s := range d.Specs {
				switch s := s.(type) {
				case *ast.TypeSpec:
					if s.Name.IsExported() {
						got = append(got, s.Name.Name)
					}
				case *ast.ValueSpec:
					for _, n := range s.Names {
						if n.IsExported() {
							got = append(got, n.Name)
						}
					}
				}
			}
		}
	}
	sort.Strings(got)
	sort.Strings(want)
	if strings.Join(got, " ") != strings.Join(want, " ") {
		t.Errorf("filter.go exports\n  %v\nwant exactly\n  %v\nA new exported name needs a decision about whether it belongs in the shipped API.", got, want)
	}
}

func TestFilterHasNoSwitchesOrSwappableState(t *testing.T) {
	f := parseNonTest(t, "filter.go")
	for _, imp := range f.Imports {
		switch strings.Trim(imp.Path.Value, `"`) {
		case "os", "testing", "flag", "runtime/debug", "unsafe":
			t.Errorf("filter.go imports %s: the filter must not read its settings from the process", imp.Path.Value)
		}
	}
	allowedVars := map[string]bool{"scratch": true, "dropReasonNames": true, "ErrBadPeer": true, "ErrBadTTL": true, "ErrTooManyEntries": true}
	for _, d := range f.Decls {
		gd, ok := d.(*ast.GenDecl)
		if !ok || gd.Tok != token.VAR {
			continue
		}
		for _, s := range gd.Specs {
			for _, n := range s.(*ast.ValueSpec).Names {
				if !allowedVars[n.Name] {
					t.Errorf("filter.go has a package-level var %s; shipped state belongs in PacketFilter fields", n.Name)
				}
			}
		}
	}
	// No file in the package carries a build constraint other than the OS ones.
	files, _ := filepath.Glob("*.go")
	for _, name := range files {
		if strings.HasSuffix(name, "_test.go") {
			continue
		}
		file := parseNonTest(t, name)
		for _, cg := range file.Comments {
			for _, c := range cg.List {
				if strings.HasPrefix(c.Text, "//go:build") && !strings.Contains(c.Text, "linux") && !strings.Contains(c.Text, "windows") && !strings.Contains(c.Text, "darwin") && !strings.Contains(c.Text, "unix") {
					t.Errorf("%s has an unexpected build constraint: %s", name, c.Text)
				}
			}
		}
	}
}

// The harness build tag must not change this package's files at all.
func TestHarnessTagDoesNotChangeTheTunnelPackage(t *testing.T) {
	def, err := build.Default.ImportDir(".", 0)
	if err != nil {
		t.Fatal(err)
	}
	ctx := build.Default
	ctx.BuildTags = []string{"meshlive_filter"}
	tagged, err := ctx.ImportDir(".", 0)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Join(def.GoFiles, " ") != strings.Join(tagged.GoFiles, " ") {
		t.Errorf("the meshlive_filter tag changes internal/tunnel: default %v, tagged %v", def.GoFiles, tagged.GoFiles)
	}
}
