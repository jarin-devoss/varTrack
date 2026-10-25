package commands

import (
	"context"
	"fmt"
	"os"
	"path/filepath"

	"github.com/spf13/cobra"
	"github.com/vartrack/vtctl/internal/client"
)

func newValidateConfigCmd() *cobra.Command {
	var (
		tenantID string
		jsonOut  bool
	)

	cmd := &cobra.Command{
		Use:   "validate-config <file>",
		Short: "Check a vartrack.json or bundle.json the way a sync would read it",
		Long: `Check a vartrack.json (also .github/, .gitea/ or .gitlab/vartrack.json) or a
tenant's bundle.json before pushing it.

It reports:
  errors      what a sync rejects: invalid JSON, a value of the wrong type, a
              preset that cannot be read, a rule config.cue does not have
  warnings    what a sync ignores: keys only config.cue sets, removed options
  migrations  options under a name that changed; a sync still reads them

The file's name says what it is. No data is written and nothing is synced; use
this in CI to catch a bad vartrack.json before merging.

Examples:
  vt validate-config vartrack.json
  vt validate-config schemas/bundle.json --tenant payments`,
		Args: cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			content, err := os.ReadFile(args[0])
			if err != nil {
				return fmt.Errorf("read file %s: %w", args[0], err)
			}

			gw, err := buildClient()
			if err != nil {
				return err
			}

			resp, err := gw.ValidateConfig(context.Background(), client.ValidateConfigRequest{
				FilePath: filepath.ToSlash(args[0]),
				Content:  string(content),
				TenantID: tenantID,
			})
			if err != nil {
				return fmt.Errorf("validate-config: %w", err)
			}

			if jsonOut {
				if err := printJSON(resp); err != nil {
					return err
				}
			} else {
				printConfigResult(resp)
			}
			if len(resp.Errors) > 0 {
				return fmt.Errorf("%s is not valid", filepath.Base(args[0]))
			}
			return nil
		},
	}

	cmd.Flags().StringVar(&tenantID, "tenant", os.Getenv("VARTRACK_TENANT"), "Tenant ID (whose rules and presets a bundle.json is checked against)")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "Output results as JSON")

	return cmd
}

func printConfigResult(resp *client.ValidateConfigResponse) {
	icon := "✓"
	switch {
	case len(resp.Errors) > 0:
		icon = "✗"
	case len(resp.Warnings) > 0 || len(resp.Migrations) > 0:
		icon = "⚠"
	}
	fmt.Printf("%s %d errors, %d warnings, %d migrations\n", icon, len(resp.Errors), len(resp.Warnings), len(resp.Migrations))
	for _, group := range []struct {
		label string
		lines []string
	}{{"error", resp.Errors}, {"warning", resp.Warnings}, {"migration", resp.Migrations}} {
		for _, line := range group.lines {
			fmt.Printf("  %s: %s\n", group.label, line)
		}
	}
}
