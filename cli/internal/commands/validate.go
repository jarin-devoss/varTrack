package commands

import (
	"context"
	"fmt"
	"os"
	"path/filepath"

	"github.com/spf13/cobra"
	"github.com/vartrack/vtctl/internal/client"
)

func newValidateCmd() *cobra.Command {
	var (
		filePath   string
		datasource string
		tenantID   string
		format     string
		jsonOut    bool
	)

	cmd := &cobra.Command{
		Use:   "validate",
		Short: "Validate a config file against its CUE schema",
		Long: `Parse a config file and validate it against the CUE schema in the schema
registry (when a datasource is specified).  No data is written to any sink.

Use this in CI pipelines to catch schema violations before merging.

Examples:
  vt validate --file configs/app.yaml --datasource mongo
  vt validate --file settings.json --datasource redis --tenant myapp`,
		RunE: func(cmd *cobra.Command, args []string) error {
			if filePath == "" {
				return fmt.Errorf("--file is required")
			}

			content, err := os.ReadFile(filePath)
			if err != nil {
				return fmt.Errorf("read file %s: %w", filePath, err)
			}

			if format == "" {
				format = detectFormat(filePath)
			}

			gw, err := buildClient()
			if err != nil {
				return err
			}

			resp, err := gw.Validate(context.Background(), client.ValidateRequest{
				FilePath:   filepath.ToSlash(filePath),
				Content:    string(content),
				Format:     format,
				Datasource: datasource,
				TenantID:   tenantID,
			})
			if err != nil {
				return fmt.Errorf("validate: %w", err)
			}

			if jsonOut {
				return printJSON(resp)
			}

			icon := map[string]string{
				"ok":     "✓",
				"warn":   "⚠",
				"failed": "✗",
			}[resp.Status]

			fmt.Printf("%s Validation %s — %d keys\n", icon, resp.Status, resp.KeyCount)
			for _, msg := range resp.Messages {
				fmt.Printf("  %s\n", msg)
			}

			if resp.Status == "failed" {
				return fmt.Errorf("validation failed")
			}
			return nil
		},
	}

	cmd.Flags().StringVar(&filePath, "file", "", "Path to the config file")
	cmd.Flags().StringVar(&datasource, "datasource", "", "Datasource name (used to select the schema)")
	cmd.Flags().StringVar(&tenantID, "tenant", os.Getenv("VARTRACK_TENANT"), "Tenant ID")
	cmd.Flags().StringVar(&format, "format", "", "File format (auto-detected from extension)")
	cmd.Flags().BoolVar(&jsonOut, "json", false, "Output results as JSON")

	return cmd
}
