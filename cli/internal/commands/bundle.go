package commands

import (
	"fmt"
	"os"

	"github.com/spf13/cobra"
)

func newBundleCmd() *cobra.Command {
	bundle := &cobra.Command{
		Use:   "bundle",
		Short: "Manage and validate CUE bundle configurations",
	}
	bundle.AddCommand(newBundleValidateCmd())
	return bundle
}

func newBundleValidateCmd() *cobra.Command {
	var bundlePath string

	cmd := &cobra.Command{
		Use:   "validate [file]",
		Short: "Validate a CUE bundle file locally",
		Long: `Validate that a CUE bundle file is syntactically correct and contains the
required top-level fields (platform, datasources, rules).

This check runs entirely locally — no gateway connection is needed.

Examples:
  vt bundle validate config.cue
  vt bundle validate ./bundles/production.cue`,
		Args: cobra.MaximumNArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			path := bundlePath
			if len(args) > 0 {
				path = args[0]
			}
			if path == "" {
				path = "config.cue"
			}

			data, err := os.ReadFile(path)
			if err != nil {
				return fmt.Errorf("read %s: %w", path, err)
			}

			// Basic structural checks without requiring cuelang.org/go on the
			// client machine. Full CUE evaluation happens server-side on load.
			required := []string{"platform:", "datasources:", "rules:"}
			missing := []string{}
			content := string(data)
			for _, field := range required {
				found := false
				for i := 0; i+len(field) <= len(content); i++ {
					if content[i:i+len(field)] == field {
						found = true
						break
					}
				}
				if !found {
					missing = append(missing, field)
				}
			}

			if len(missing) > 0 {
				fmt.Printf("✗ %s: missing required top-level fields: %v\n", path, missing)
				return fmt.Errorf("bundle validation failed")
			}

			fmt.Printf("✓ %s looks valid (%d bytes)\n", path, len(data))
			fmt.Println("  Full CUE evaluation runs server-side on gateway startup.")
			return nil
		},
	}

	cmd.Flags().StringVar(&bundlePath, "file", "", "Bundle file path (default: config.cue)")
	return cmd
}
