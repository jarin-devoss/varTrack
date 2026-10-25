package commands

import (
	"context"
	"fmt"
	"os"
	"path/filepath"

	"github.com/spf13/cobra"
	"github.com/vartrack/vtctl/internal/client"
)

// configFileNames are the names a repository's settings file may have, in the
// order the orchestrator looks for them (VARTRACK_JSON_NAMES in
// orchestrator-service/app/pipeline/repo_overrides.py): the first one a
// repository has is the one a sync reads.
var configFileNames = []string{
	"vartrack.json",
	".github/vartrack.json",
	".gitea/vartrack.json",
	".gitlab/vartrack.json",
}

// configCheck is one file's validation result, as `--json` prints it.
type configCheck struct {
	File       string   `json:"file"`
	Errors     []string `json:"errors"`
	Warnings   []string `json:"warnings"`
	Migrations []string `json:"migrations"`
}

// discoverConfigFiles returns the settings files that exist under dir, in the
// order of configFileNames.
func discoverConfigFiles(dir string) []string {
	var found []string
	for _, name := range configFileNames {
		path := filepath.Join(dir, filepath.FromSlash(name))
		if info, err := os.Stat(path); err == nil && !info.IsDir() {
			found = append(found, name)
		}
	}
	return found
}

// exitCode is the process exit code for a run: 1 when any file has errors or
// warnings (a warning is a setting a sync ignores, so the file does not do what
// it says); with strict, also when any file still uses an option under its old
// name. Otherwise 0.
func exitCode(results []configCheck, strict bool) int {
	for _, r := range results {
		if len(r.Errors) > 0 || len(r.Warnings) > 0 || (strict && len(r.Migrations) > 0) {
			return 1
		}
	}
	return 0
}

func newValidateConfigCmd() *cobra.Command {
	var (
		tenantID string
		jsonOut  bool
		strict   bool
	)

	cmd := &cobra.Command{
		Use:   "validate-config [file...]",
		Short: "Check a vartrack.json or bundle.json the way a sync would read it",
		Long: `Check a vartrack.json (also .github/, .gitea/ or .gitlab/vartrack.json) or a
tenant's bundle.json before pushing it.

With no file named, it checks the vartrack.json files in the current directory
(vartrack.json, .github/vartrack.json, .gitea/vartrack.json, .gitlab/vartrack.json)
and has nothing to do when there are none.

It reports:
  errors      what a sync rejects: invalid JSON, a value of the wrong type, a
              preset that cannot be read, a rule config.cue does not have
  warnings    what a sync ignores: keys only config.cue sets, removed options
  migrations  options under a name that changed; a sync still reads them

The exit code is 1 when any file has errors or warnings. With --strict it is
also 1 when a file has migrations. The file's name says what it is. No data is
written and nothing is synced; use this in CI to catch a bad vartrack.json
before merging.

Examples:
  vt validate-config
  vt validate-config vartrack.json
  vt validate-config --strict
  vt validate-config schemas/bundle.json --tenant payments`,
		Args: cobra.ArbitraryArgs,
		RunE: func(cmd *cobra.Command, args []string) error {
			files := args
			if len(files) == 0 {
				files = discoverConfigFiles(".")
			}
			if len(files) == 0 {
				if jsonOut {
					return printJSON([]configCheck{})
				}
				fmt.Println("no vartrack.json found")
				return nil
			}

			gw, err := buildClient()
			if err != nil {
				return err
			}

			results := make([]configCheck, 0, len(files))
			for _, file := range files {
				content, err := os.ReadFile(file)
				if err != nil {
					return fmt.Errorf("read file %s: %w", file, err)
				}
				resp, err := gw.ValidateConfig(context.Background(), client.ValidateConfigRequest{
					FilePath: filepath.ToSlash(file),
					Content:  string(content),
					TenantID: tenantID,
				})
				if err != nil {
					return fmt.Errorf("validate-config %s: %w", file, err)
				}
				results = append(results, configCheck{
					File:       filepath.ToSlash(file),
					Errors:     orEmpty(resp.Errors),
					Warnings:   orEmpty(resp.Warnings),
					Migrations: orEmpty(resp.Migrations),
				})
			}

			if jsonOut {
				if err := printJSON(results); err != nil {
					return err
				}
			} else {
				for _, r := range results {
					printConfigResult(r)
				}
			}
			if exitCode(results, strict) != 0 {
				return fmt.Errorf("%s", failureSummary(results, strict))
			}
			return nil
		},
	}

	cmd.Flags().StringVar(&tenantID, "tenant", os.Getenv("VARTRACK_TENANT"), "Tenant ID (whose rules and presets a bundle.json is checked against)")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "Output results as JSON")
	cmd.Flags().BoolVar(&strict, "strict", false, "Also fail when a file uses options under a name that changed (migrations)")

	return cmd
}

func orEmpty(lines []string) []string {
	if lines == nil {
		return []string{}
	}
	return lines
}

// failureSummary names the files that made the run fail.
func failureSummary(results []configCheck, strict bool) string {
	var bad []string
	for _, r := range results {
		if exitCode([]configCheck{r}, strict) != 0 {
			bad = append(bad, r.File)
		}
	}
	if len(bad) == 1 {
		return fmt.Sprintf("%s is not valid", bad[0])
	}
	return fmt.Sprintf("%d of %d files are not valid: %v", len(bad), len(results), bad)
}

func printConfigResult(r configCheck) {
	icon := "✓"
	switch {
	case len(r.Errors) > 0:
		icon = "✗"
	case len(r.Warnings) > 0 || len(r.Migrations) > 0:
		icon = "⚠"
	}
	fmt.Printf("%s %s: %d errors, %d warnings, %d migrations\n", icon, r.File, len(r.Errors), len(r.Warnings), len(r.Migrations))
	for _, group := range []struct {
		label string
		lines []string
	}{{"error", r.Errors}, {"warning", r.Warnings}, {"migration", r.Migrations}} {
		for _, line := range group.lines {
			fmt.Printf("  %s: %s\n", group.label, line)
		}
	}
}
