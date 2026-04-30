package commands

import (
	"fmt"

	"github.com/spf13/cobra"
	"github.com/vartrack/vtctl/internal/config"
	"gopkg.in/yaml.v3"
)

func newConfigCmd() *cobra.Command {
	cfgCmd := &cobra.Command{
		Use:   "config",
		Short: "View and manage vt configuration",
	}

	cfgCmd.AddCommand(
		newConfigViewCmd(),
		newConfigSetContextCmd(),
		newConfigUseContextCmd(),
	)
	return cfgCmd
}

func newConfigViewCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "view",
		Short: "Print the current configuration (tokens redacted)",
		RunE: func(cmd *cobra.Command, args []string) error {
			redacted := *cfg
			safe := make([]config.Context, len(cfg.Contexts))
			for i, ctx := range cfg.Contexts {
				c := ctx
				if c.Token != "" {
					c.Token = "[redacted]"
				}
				if c.RefreshToken != "" {
					c.RefreshToken = "[redacted]"
				}
				safe[i] = c
			}
			redacted.Contexts = safe

			out, err := yaml.Marshal(redacted)
			if err != nil {
				return err
			}
			fmt.Print(string(out))
			return nil
		},
	}
}

func newConfigSetContextCmd() *cobra.Command {
	var (
		server   string
		tenantID string
		insecure bool
	)

	cmd := &cobra.Command{
		Use:   "set-context <name>",
		Short: "Create or update a named context",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			name := args[0]

			ctx := config.Context{Name: name}
			// Preserve existing values for fields not explicitly set.
			for _, existing := range cfg.Contexts {
				if existing.Name == name {
					ctx = existing
					break
				}
			}
			if server != "" {
				ctx.Server = server
			}
			if tenantID != "" {
				ctx.TenantID = tenantID
			}
			if cmd.Flags().Changed("insecure") {
				ctx.Insecure = insecure
			}

			cfg.UpsertContext(ctx)
			return config.Save(cfgPath, cfg)
		},
	}

	cmd.Flags().StringVar(&server, "server", "", "Gateway server URL")
	cmd.Flags().StringVar(&tenantID, "tenant", "", "Default tenant ID")
	cmd.Flags().BoolVar(&insecure, "insecure", false, "Skip TLS verification")
	return cmd
}

func newConfigUseContextCmd() *cobra.Command {
	return &cobra.Command{
		Use:   "use-context <name>",
		Short: "Switch the active context",
		Args:  cobra.ExactArgs(1),
		RunE: func(cmd *cobra.Command, args []string) error {
			name := args[0]
			found := false
			for _, ctx := range cfg.Contexts {
				if ctx.Name == name {
					found = true
					break
				}
			}
			if !found {
				return fmt.Errorf("context %q not found — run 'vt login' to create it", name)
			}
			cfg.ActiveContext = name
			if err := config.Save(cfgPath, cfg); err != nil {
				return err
			}
			fmt.Printf("Switched to context %q\n", name)
			return nil
		},
	}
}
