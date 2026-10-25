// Package models defines the core domain types for the gateway service,
// including platforms, secret managers, rules, and the configuration bundle.
package models

import (
	"context"
	"errors"
	"fmt"
	"hash"

	"gateway-service/internal/driver"
	"gateway-service/internal/secrets"

	pb_models "vartrack/libs/gen/vartrack/v1/models"
)

// Platform abstracts a Git hosting provider (GitHub, GitLab, Bitbucket).
// Each driver implements Open to authenticate and prepare an HTTP client.
type Platform interface {
	EventTypeHeader() string
	SignatureHeader() string
	IsPushEvent(eventType string) bool
	IsPREvent(eventType string) bool
	WebhookHasher() hash.Hash
	VerifySignature(mac hash.Hash, signatureHeader string) bool
	ConstructCloneURL(repo string) string
	Auth(ctx context.Context) error
	Open(ctx context.Context, config *pb_models.Platform, resolver secrets.Resolver, managerName string) (Platform, error)
	Close(ctx context.Context) error
	Repos(ctx context.Context, patterns []string) ([]string, error)
	Secret() string

	// The Git API as the holder of token sees it. The CLI API authorizes by
	// Git permission: whoever may push to a rule's repository may sync it.
	CallerLogin(ctx context.Context, token string) (string, error)
	CallerPermission(ctx context.Context, token, repo string) (RepoPermission, error)
	CallerRepos(ctx context.Context, token string) (map[string]RepoPermission, error)
	// CallerCanPush reports whether the holder of token may push to branch
	// directly, branch protection included.
	CallerCanPush(ctx context.Context, token, repo, branch string) (bool, error)
}

// RepoPermission is what a person may do in a repository, as the Git
// platform reports it for their token.
type RepoPermission struct {
	Read  bool
	Write bool
	Admin bool
}

// Or is what either p or o allows.
func (p RepoPermission) Or(o RepoPermission) RepoPermission {
	return RepoPermission{Read: p.Read || o.Read, Write: p.Write || o.Write, Admin: p.Admin || o.Admin}
}

// ErrTokenRejected: the Git platform does not accept the caller's token.
var ErrTokenRejected = errors.New("the Git platform rejected the token")

// PlatformConfig bundles the inputs needed to create a Platform instance.
type PlatformConfig struct {
	Platform    *pb_models.Platform
	Resolver    secrets.Resolver
	ManagerName string
}

// PlatformRegistry is the global driver registry for platform implementations.
var PlatformRegistry = driver.NewRegistry[Platform, PlatformConfig](
	"platform",
	func(d Platform, ctx context.Context, config PlatformConfig) (Platform, error) {
		return d.Open(ctx, config.Platform, config.Resolver, config.ManagerName)
	},
)

// PlatformFactory creates Platform instances from PlatformConfig via the registry.
var PlatformFactory = driver.NewFactory(
	PlatformRegistry,
	func(c PlatformConfig) string {
		if c.Platform == nil {
			return ""
		}
		// Drivers are registered by type; a tagged platform ("gitea-team") still opens the "gitea" driver.
		return PlatformDriverName(c.Platform)
	},
	func(c PlatformConfig) error {
		if c.Platform == nil {
			return fmt.Errorf("platform config is required")
		}
		return nil
	},
	"platform",
)

// PlatformName returns the resolved name for a platform.
// If a tag is set, the name is "{type}-{tag}" (e.g. "github-dr").
// Otherwise, it falls back to the type name (e.g. "github").
func PlatformName(p *pb_models.Platform) string {
	switch config := p.Config.(type) {
	case *pb_models.Platform_Github:
		return driver.ResolveTagName("github", config.Github.GetTag())
	case *pb_models.Platform_Gitea:
		return driver.ResolveTagName("gitea", config.Gitea.GetTag())
	default:
		return ""
	}
}

// PlatformDriverName returns the driver type name (e.g. "github")
// used for registry lookups, independent of the tag.
func PlatformDriverName(p *pb_models.Platform) string {
	switch p.Config.(type) {
	case *pb_models.Platform_Github:
		return "github"
	case *pb_models.Platform_Gitea:
		return "gitea"
	default:
		return ""
	}
}
