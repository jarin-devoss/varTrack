package models

import (
	"context"
	"errors"
	"fmt"
	"sync"

	"gateway-service/internal/driver"
	"gateway-service/internal/secrets"

	pb "gateway-service/internal/gen/proto/go/vartrack/v1/models"
)

// Bundle holds the runtime state for the loaded bundle configuration.
// It lazily initializes platforms and secret managers on first use and
// caches them for subsequent requests.
type Bundle struct {
	bundle               *pb.Bundle
	platformFactory      *driver.Factory[Platform, PlatformConfig]
	secretManagerFactory *driver.Factory[SecretManager, *pb.SecretManager]
	secretRefResolver    *secrets.CachingRefResolver
	platforms            map[string]Platform
	secretManagers       map[string]SecretManager
	mu                   sync.RWMutex
}

// NewBundle creates a Bundle from a protobuf Bundle message.
func NewBundle(pbBundle *pb.Bundle) *Bundle {
	b := &Bundle{
		bundle:               pbBundle,
		platformFactory:      PlatformFactory,
		secretManagerFactory: SecretManagerFactory,
		platforms:            make(map[string]Platform),
		secretManagers:       make(map[string]SecretManager),
	}
	b.secretRefResolver = secrets.NewCachingRefResolver(
		secrets.NewRefResolver(
			func(ctx context.Context, name string) (secrets.Fetcher, error) {
				return b.GetSecretManager(ctx, name)
			},
		),
		secrets.DefaultCacheConfig(),
	)
	return b
}

// GetSchemaRegistry returns the schema registry config, or nil if not configured.
func (s *Bundle) GetSchemaRegistry() *pb.SchemaRegistry {
	return s.bundle.GetSchemaRegistry()
}

// FindRule returns the rule matching the given platform and datasource.
func (s *Bundle) FindRule(platformName, datasourceName string) *pb.Rule {
	for _, r := range s.bundle.Rules {
		if r.Platform == platformName && r.Datasource == datasourceName {
			return r
		}
	}
	return nil
}

// SecretManagerNameForRule returns the secret manager name for a rule.
func (s *Bundle) SecretManagerNameForRule(platformName, datasourceName string) string {
	rule := s.FindRule(platformName, datasourceName)
	if rule == nil {
		return ""
	}
	return rule.GetSecretManager()
}

// GetPlatform returns a cached or newly created platform instance.
// Uses double-checked locking (RLock then Lock) to minimise contention.
func (s *Bundle) GetPlatform(ctx context.Context, name, managerName string) (Platform, error) {
	cacheKey := name
	if managerName != "" {
		cacheKey = name + ":" + managerName
	}

	s.mu.RLock()
	if plat, ok := s.platforms[cacheKey]; ok {
		s.mu.RUnlock()
		return plat, nil
	}
	s.mu.RUnlock()

	s.mu.Lock()
	defer s.mu.Unlock()

	if plat, ok := s.platforms[cacheKey]; ok {
		return plat, nil
	}

	var config *pb.Platform
	for _, p := range s.bundle.Platforms {
		if PlatformName(p) == name {
			config = p
			break
		}
	}

	if config == nil {
		return nil, fmt.Errorf("platform %q not found in bundle configuration", name)
	}

	plat, err := s.platformFactory.Get(ctx, PlatformConfig{
		Platform:    config,
		Resolver:    s.secretRefResolver,
		ManagerName: managerName,
	})
	if err != nil {
		return nil, fmt.Errorf("failed to create platform %q: %w", name, err)
	}

	s.platforms[cacheKey] = plat
	return plat, nil
}

// GetPlatformForRule resolves the platform for a given rule.
func (s *Bundle) GetPlatformForRule(ctx context.Context, platformName, datasourceName string) (Platform, error) {
	managerName := s.SecretManagerNameForRule(platformName, datasourceName)
	return s.GetPlatform(ctx, platformName, managerName)
}

// GetSecretManager returns a cached or newly created secret manager.
func (s *Bundle) GetSecretManager(ctx context.Context, name string) (SecretManager, error) {
	s.mu.RLock()
	if sm, ok := s.secretManagers[name]; ok {
		s.mu.RUnlock()
		return sm, nil
	}
	s.mu.RUnlock()

	s.mu.Lock()
	defer s.mu.Unlock()

	if sm, ok := s.secretManagers[name]; ok {
		return sm, nil
	}

	var config *pb.SecretManager
	for _, sm := range s.bundle.SecretManagers {
		if SecretManagerName(sm) == name {
			config = sm
			break
		}
	}

	if config == nil {
		return nil, fmt.Errorf("secret manager %q not found in bundle configuration", name)
	}

	sm, err := s.secretManagerFactory.Get(ctx, config)
	if err != nil {
		return nil, fmt.Errorf("failed to create secret manager %q: %w", name, err)
	}

	s.secretManagers[name] = sm
	return sm, nil
}

// Close releases all platform and secret manager resources.
func (s *Bundle) Close(ctx context.Context) error {
	s.mu.Lock()
	defer s.mu.Unlock()

	var errs []error
	for name, plat := range s.platforms {
		if err := plat.Close(ctx); err != nil {
			errs = append(errs, fmt.Errorf("close platform %q: %w", name, err))
		}
	}
	for name, sm := range s.secretManagers {
		if err := sm.Close(ctx); err != nil {
			errs = append(errs, fmt.Errorf("close secret manager %q: %w", name, err))
		}
	}
	return errors.Join(errs...)
}

// ListConfiguredPlatforms returns the names of all platforms in the bundle.
func (s *Bundle) ListConfiguredPlatforms() []string {
	names := make([]string, 0, len(s.bundle.Platforms))
	for _, p := range s.bundle.Platforms {
		if n := PlatformName(p); n != "" {
			names = append(names, n)
		}
	}
	return names
}

// ListConfiguredSecretManagers returns the names of all secret managers in the bundle.
func (s *Bundle) ListConfiguredSecretManagers() []string {
	names := make([]string, 0, len(s.bundle.SecretManagers))
	for _, sm := range s.bundle.SecretManagers {
		if n := SecretManagerName(sm); n != "" {
			names = append(names, n)
		}
	}
	return names
}

// GetBundle returns the underlying protobuf Bundle.
func (s *Bundle) GetBundle() *pb.Bundle {
	return s.bundle
}

// WebhookBodyLimit returns the configured max webhook body size in bytes.
// Falls back to 10 MiB when unset or zero.
func (s *Bundle) WebhookBodyLimit() int64 {
	const defaultLimit int64 = 10 << 20 // 10 MiB
	if v := s.bundle.GetMaxWebhookBodyBytes(); v > 0 {
		return int64(v)
	}
	return defaultLimit
}

// FindRuleByDatasource returns the first rule matching the datasource name.
func (s *Bundle) FindRuleByDatasource(datasourceName string) *pb.Rule {
	for _, r := range s.bundle.Rules {
		if r.Datasource == datasourceName {
			return r
		}
	}
	return nil
}

// GetPlatformForDatasource resolves the platform from a datasource name via the rule config.
func (s *Bundle) GetPlatformForDatasource(ctx context.Context, datasourceName string) (Platform, string, error) {
	rule := s.FindRuleByDatasource(datasourceName)
	if rule == nil {
		return nil, "", fmt.Errorf("no rule found for datasource %q", datasourceName)
	}
	platformName := rule.Platform
	managerName := rule.GetSecretManager()
	plat, err := s.GetPlatform(ctx, platformName, managerName)
	if err != nil {
		return nil, "", err
	}
	return plat, platformName, nil
}
