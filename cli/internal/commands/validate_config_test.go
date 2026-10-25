package commands

import (
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

func touch(t *testing.T, dir, name string) {
	t.Helper()
	path := filepath.Join(dir, filepath.FromSlash(name))
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(path, []byte("{}"), 0o644); err != nil {
		t.Fatal(err)
	}
}

func TestDiscoverFindsTheFilesThatExistInTheOrchestratorsOrder(t *testing.T) {
	dir := t.TempDir()
	touch(t, dir, ".gitlab/vartrack.json")
	touch(t, dir, "vartrack.json")
	touch(t, dir, ".gitea/vartrack.json")
	touch(t, dir, "other.json")

	got := discoverConfigFiles(dir)
	want := []string{"vartrack.json", ".gitea/vartrack.json", ".gitlab/vartrack.json"}
	if !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v, want %v", got, want)
	}
}

func TestDiscoverFindsNothingInAnEmptyDirectory(t *testing.T) {
	if got := discoverConfigFiles(t.TempDir()); len(got) != 0 {
		t.Fatalf("got %v, want none", got)
	}
}

func TestDiscoverIgnoresADirectoryWithTheName(t *testing.T) {
	dir := t.TempDir()
	if err := os.MkdirAll(filepath.Join(dir, "vartrack.json"), 0o755); err != nil {
		t.Fatal(err)
	}
	if got := discoverConfigFiles(dir); len(got) != 0 {
		t.Fatalf("got %v, want none", got)
	}
}

func TestExitCode(t *testing.T) {
	clean := configCheck{File: "a.json"}
	errors := configCheck{File: "b.json", Errors: []string{"bad type"}}
	warnings := configCheck{File: "c.json", Warnings: []string{"key ignored"}}
	migrations := configCheck{File: "d.json", Migrations: []string{"renamed"}}

	cases := []struct {
		name    string
		results []configCheck
		strict  bool
		want    int
	}{
		{"nothing to check", nil, false, 0},
		{"clean", []configCheck{clean}, false, 0},
		{"errors", []configCheck{errors}, false, 1},
		{"warnings", []configCheck{warnings}, false, 1},
		{"migrations pass", []configCheck{migrations}, false, 0},
		{"migrations fail when strict", []configCheck{migrations}, true, 1},
		{"strict leaves a clean file alone", []configCheck{clean}, true, 0},
		{"one failing file fails the run", []configCheck{clean, warnings, clean}, false, 1},
		{"the last failing file fails the run", []configCheck{clean, clean, errors}, false, 1},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			if got := exitCode(c.results, c.strict); got != c.want {
				t.Fatalf("exitCode = %d, want %d", got, c.want)
			}
		})
	}
}

func TestFailureSummaryNamesTheFilesThatFailed(t *testing.T) {
	results := []configCheck{
		{File: "ok.json"},
		{File: "bad.json", Errors: []string{"x"}},
		{File: "warn.json", Warnings: []string{"y"}},
		{File: "old.json", Migrations: []string{"z"}},
	}
	if got := failureSummary(results[:2], false); got != "bad.json is not valid" {
		t.Fatalf("got %q", got)
	}
	got := failureSummary(results, false)
	if !strings.Contains(got, "2 of 4") || !strings.Contains(got, "bad.json") || !strings.Contains(got, "warn.json") ||
		strings.Contains(got, "old.json") {
		t.Fatalf("got %q", got)
	}
	if !strings.Contains(failureSummary(results, true), "old.json") {
		t.Fatal("with --strict a file with migrations is named too")
	}
}
