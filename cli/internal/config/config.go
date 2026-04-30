// Package config manages the vt local configuration stored in
// ~/.config/vt/config.yaml.
package config

import (
	"fmt"
	"os"
	"path/filepath"

	"gopkg.in/yaml.v3"
)

// Context represents a named server connection profile.
type Context struct {
	Name         string `yaml:"name"`
	Server       string `yaml:"server"`
	Token        string `yaml:"token,omitempty"`
	RefreshToken string `yaml:"refresh_token,omitempty"`
	OIDCIssuer   string `yaml:"oidc_issuer,omitempty"`
	OIDCClientID string `yaml:"oidc_client_id,omitempty"`
	TenantID     string `yaml:"tenant_id,omitempty"`
	Insecure     bool   `yaml:"insecure,omitempty"`
}

// Config is the top-level configuration file model.
type Config struct {
	ActiveContext string    `yaml:"current_context"`
	Contexts      []Context `yaml:"contexts"`
}

// DefaultPath returns the default config file path.
func DefaultPath() string {
	home, err := os.UserHomeDir()
	if err != nil {
		return "vt.yaml"
	}
	return filepath.Join(home, ".config", "vt", "config.yaml")
}

// Load reads the config from disk, returning an empty Config when the file
// does not exist yet.
func Load(path string) (*Config, error) {
	data, err := os.ReadFile(path)
	if os.IsNotExist(err) {
		return &Config{}, nil
	}
	if err != nil {
		return nil, fmt.Errorf("read config: %w", err)
	}

	var cfg Config
	if err := yaml.Unmarshal(data, &cfg); err != nil {
		return nil, fmt.Errorf("parse config: %w", err)
	}
	return &cfg, nil
}

// Save writes the config back to disk, creating the parent directory when
// it does not exist.
func Save(path string, cfg *Config) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return fmt.Errorf("create config dir: %w", err)
	}

	data, err := yaml.Marshal(cfg)
	if err != nil {
		return fmt.Errorf("marshal config: %w", err)
	}

	if err := os.WriteFile(path, data, 0o600); err != nil {
		return fmt.Errorf("write config: %w", err)
	}
	return nil
}

// ActiveCtx returns the active context or an error when none is set.
func (c *Config) ActiveCtx() (*Context, error) {
	name := c.contextName()
	if name == "" {
		return nil, fmt.Errorf("no current context — run 'vt login'")
	}
	for i := range c.Contexts {
		if c.Contexts[i].Name == name {
			return &c.Contexts[i], nil
		}
	}
	return nil, fmt.Errorf("context %q not found — run 'vt login'", name)
}

// contextName returns the active context name, preferring the
// VARTRACK_CONTEXT environment variable over the saved value.
func (c *Config) contextName() string {
	if v := os.Getenv("VARTRACK_CONTEXT"); v != "" {
		return v
	}
	return c.ActiveContext
}

// UpsertContext adds or replaces a context by name.
func (c *Config) UpsertContext(ctx Context) {
	for i := range c.Contexts {
		if c.Contexts[i].Name == ctx.Name {
			c.Contexts[i] = ctx
			return
		}
	}
	c.Contexts = append(c.Contexts, ctx)
}

// RemoveContext deletes a context by name.  Returns true when found.
func (c *Config) RemoveContext(name string) bool {
	for i, ctx := range c.Contexts {
		if ctx.Name == name {
			c.Contexts = append(c.Contexts[:i], c.Contexts[i+1:]...)
			return true
		}
	}
	return false
}
