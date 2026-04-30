// Package commands wires all vtctl subcommands under the root Cobra command.
package commands

import (
	"fmt"
	"os"

	"github.com/spf13/cobra"
	"github.com/vartrack/vtctl/internal/client"
	"github.com/vartrack/vtctl/internal/config"
)

var (
	cfgPath string
	cfg     *config.Config
)

// Root returns the top-level vt command.
func Root() *cobra.Command {
	root := &cobra.Command{
		Use:   "vt",
		Short: "VarTrack CLI — push config to datasources from CI/CD or local",
		Long: `vt is the command-line interface for VarTrack.

It lets you sync local config files to any datasource defined in your
CUE bundle without needing a git push, making it ideal for CI/CD pipelines
and one-off manual deployments.

Authentication is handled via OIDC (SSO / Active Directory) or a static
token for non-interactive CI environments.`,
		SilenceUsage:  true,
		SilenceErrors: true,
		PersistentPreRunE: func(cmd *cobra.Command, args []string) error {
			if cfgPath == "" {
				cfgPath = config.DefaultPath()
			}
			var err error
			cfg, err = config.Load(cfgPath)
			if err != nil {
				return fmt.Errorf("load config: %w", err)
			}
			return nil
		},
	}

	root.PersistentFlags().StringVar(&cfgPath, "config", "", "vt config file (default ~/.config/vt/config.yaml)")

	root.AddCommand(
		newLoginCmd(),
		newLogoutCmd(),
		newSyncCmd(),
		newValidateCmd(),
		newTaskCmd(),
		newBundleCmd(),
		newConfigCmd(),
		newVersionCmd(),
	)
	return root
}

// buildClient constructs a gateway client from the active context.
// Env var VARTRACK_TOKEN overrides the stored token (for CI/CD).
// Env var VARTRACK_SERVER overrides the stored server URL.
func buildClient() (*client.Client, error) {
	token := os.Getenv("VARTRACK_TOKEN")
	server := os.Getenv("VARTRACK_SERVER")

	if token != "" && server != "" {
		tenantID := os.Getenv("VARTRACK_TENANT")
		return client.New(server, token, tenantID, false), nil
	}

	ctx, err := cfg.ActiveCtx()
	if err != nil {
		return nil, err
	}

	if token == "" {
		token = ctx.Token
	}
	if server == "" {
		server = ctx.Server
	}
	return client.New(server, token, ctx.TenantID, ctx.Insecure), nil
}
