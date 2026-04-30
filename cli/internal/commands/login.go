package commands

import (
	"context"
	"fmt"
	"os"
	"strings"
	"time"

	"github.com/spf13/cobra"
	"github.com/vartrack/vtctl/internal/auth"
	"github.com/vartrack/vtctl/internal/config"
)

func newLoginCmd() *cobra.Command {
	var (
		server       string
		contextName  string
		token        string
		oidcIssuer   string
		oidcClientID string
		tenantID     string
		insecure     bool
		callbackPort int
	)

	cmd := &cobra.Command{
		Use:   "login",
		Short: "Authenticate with a VarTrack gateway",
		Long: `Authenticate using OIDC (browser-based SSO / Active Directory) or a static
token (for CI environments where browser access is unavailable).

OIDC flow — opens a browser, performs PKCE-secured authorization, stores
tokens in ~/.config/vt/config.yaml.

Static token — use --token or set VARTRACK_TOKEN to skip browser auth.
This is the recommended approach for CI/CD pipelines.

Examples:
  # OIDC browser login
  vt login --server https://gateway.example.com \
              --oidc-issuer https://accounts.google.com \
              --oidc-client-id my-app-client-id

  # Azure Active Directory
  vt login --server https://gateway.example.com \
              --oidc-issuer https://login.microsoftonline.com/<tenant-id>/v2.0 \
              --oidc-client-id <azure-app-client-id>

  # Static token (CI/CD)
  vt login --server https://gateway.example.com --token eyJ...`,
		RunE: func(cmd *cobra.Command, args []string) error {
			if server == "" {
				return fmt.Errorf("--server is required")
			}
			server = strings.TrimRight(server, "/")

			if contextName == "" {
				// Derive context name from server hostname.
				contextName = serverToContextName(server)
			}

			var storedToken, refreshToken string

			if token != "" {
				// Static token mode — no browser required.
				storedToken = token
				fmt.Printf("Logged in as static token user on %s\n", server)
			} else {
				// OIDC browser flow.
				if oidcIssuer == "" {
					return fmt.Errorf("--oidc-issuer is required for OIDC login (or use --token for static auth)")
				}
				if oidcClientID == "" {
					return fmt.Errorf("--oidc-client-id is required for OIDC login")
				}

				ctx, cancel := context.WithTimeout(context.Background(), 6*time.Minute)
				defer cancel()

				tok, err := auth.Login(ctx, auth.OIDCConfig{
					IssuerURL:    oidcIssuer,
					ClientID:     oidcClientID,
					CallbackPort: callbackPort,
				})
				if err != nil {
					return fmt.Errorf("login failed: %w", err)
				}
				storedToken = tok.IDToken
				if storedToken == "" {
					storedToken = tok.AccessToken
				}
				refreshToken = tok.RefreshToken
				fmt.Printf("Successfully logged in via OIDC on %s\n", server)
			}

			newCtx := config.Context{
				Name:         contextName,
				Server:       server,
				Token:        storedToken,
				RefreshToken: refreshToken,
				OIDCIssuer:   oidcIssuer,
				OIDCClientID: oidcClientID,
				TenantID:     tenantID,
				Insecure:     insecure,
			}

			cfg.UpsertContext(newCtx)
			cfg.ActiveContext = contextName

			if err := config.Save(cfgPath, cfg); err != nil {
				return fmt.Errorf("save config: %w", err)
			}

			fmt.Printf("Context %q saved — use 'vt config view' to inspect\n", contextName)
			return nil
		},
	}

	cmd.Flags().StringVar(&server, "server", os.Getenv("VARTRACK_SERVER"), "Gateway server URL")
	cmd.Flags().StringVar(&contextName, "context", "", "Context name (derived from server hostname when omitted)")
	cmd.Flags().StringVar(&token, "token", os.Getenv("VARTRACK_TOKEN"), "Static bearer token (skips browser auth)")
	cmd.Flags().StringVar(&oidcIssuer, "oidc-issuer", "", "OIDC provider issuer URL")
	cmd.Flags().StringVar(&oidcClientID, "oidc-client-id", "", "OIDC client ID")
	cmd.Flags().StringVar(&tenantID, "tenant", os.Getenv("VARTRACK_TENANT"), "Default tenant ID for this context")
	cmd.Flags().BoolVar(&insecure, "insecure", false, "Skip TLS certificate verification")
	cmd.Flags().IntVar(&callbackPort, "callback-port", 8085, "Local port for OIDC redirect callback")

	return cmd
}

func newLogoutCmd() *cobra.Command {
	var contextName string

	cmd := &cobra.Command{
		Use:   "logout",
		Short: "Remove stored credentials for a context",
		RunE: func(cmd *cobra.Command, args []string) error {
			name := contextName
			if name == "" {
				ctx, err := cfg.ActiveCtx()
				if err != nil {
					return err
				}
				name = ctx.Name
			}

			if !cfg.RemoveContext(name) {
				return fmt.Errorf("context %q not found", name)
			}
			if cfg.ActiveContext == name {
				cfg.ActiveContext = ""
			}

			if err := config.Save(cfgPath, cfg); err != nil {
				return fmt.Errorf("save config: %w", err)
			}
			fmt.Printf("Logged out of context %q\n", name)
			return nil
		},
	}

	cmd.Flags().StringVar(&contextName, "context", "", "Context to log out of (default: current context)")
	return cmd
}

func serverToContextName(server string) string {
	name := server
	for _, prefix := range []string{"https://", "http://"} {
		name = strings.TrimPrefix(name, prefix)
	}
	name = strings.Split(name, ":")[0]
	return name
}
