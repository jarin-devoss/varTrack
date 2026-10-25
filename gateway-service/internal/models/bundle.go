package models

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strings"
	"sync"

	"gateway-service/internal/driver"
	"gateway-service/internal/secrets"

	pb "vartrack/libs/gen/vartrack/v1/models"
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
	if name == "" {
		resolved, err := s.defaultSecretManagerName()
		if err != nil {
			return nil, err
		}
		name = resolved
	}

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

// PlatformNames are the names of the bundle's platforms, in order.
func (s *Bundle) PlatformNames() []string {
	names := make([]string, 0, len(s.bundle.Platforms))
	for _, p := range s.bundle.Platforms {
		names = append(names, PlatformName(p))
	}
	return names
}

// RulesForDatasource returns every rule that writes to the datasource, one
// per platform: a rule is identified by its platform and datasource.
func (s *Bundle) RulesForDatasource(datasourceName string) []*pb.Rule {
	var rules []*pb.Rule
	for _, r := range s.bundle.Rules {
		if r.Datasource == datasourceName {
			rules = append(rules, r)
		}
	}
	return rules
}

// PlatformMismatchError: rules on several platforms write to the datasource
// and the webhook carries none of their event headers.
type PlatformMismatchError struct {
	Datasource string
	// Expected maps each platform with a rule for the datasource to the
	// event header its webhooks carry.
	Expected map[string]string
}

func (e *PlatformMismatchError) Error() string {
	var parts []string
	for name, header := range e.Expected {
		parts = append(parts, fmt.Sprintf("%s (%s)", name, header))
	}
	sort.Strings(parts)
	return fmt.Sprintf("no platform with a rule for datasource %q sent this webhook: expected an event header of %s",
		e.Datasource, strings.Join(parts, ", "))
}

// AmbiguousPlatformError: more than one platform of the same type has a rule
// for the datasource, and the webhook's headers fit them all. The signature
// is what can still tell them apart (WebhookCandidate.Signed).
type AmbiguousPlatformError struct {
	Datasource string
	Platforms  []string
	// Candidates are the platforms the headers fit, in the order of the rules.
	Candidates []WebhookCandidate
}

func (e *AmbiguousPlatformError) Error() string {
	return fmt.Sprintf("this webhook fits several platforms with a rule for datasource %q (%s): "+
		"their webhooks cannot be told apart", e.Datasource, strings.Join(e.Platforms, ", "))
}

// WebhookCandidate is a platform whose headers fit a webhook.
type WebhookCandidate struct {
	Name     string
	Platform Platform
}

// Signed reports whether body, delivered with the given request headers, is
// signed with this platform's webhook secret: the platform's own signature
// check, run on a hash of the body. A platform without a secret signs nothing.
func (c WebhookCandidate) Signed(body []byte, header func(string) string) bool {
	if c.Platform.Secret() == "" {
		return false
	}
	mac := c.Platform.WebhookHasher()
	if mac == nil {
		return false
	}
	mac.Write(body)
	return c.Platform.VerifySignature(mac, header(c.Platform.SignatureHeader()))
}

// BySignature picks, among the candidates, the only platform whose webhook
// secret signed the body. Platforms of one type share their signature scheme,
// so the secret is what tells their deliveries apart, as it is the one thing
// only the sender and its platform know. It reports false when no candidate
// signed the body, or several did (the same secret on both).
func (e *AmbiguousPlatformError) BySignature(body []byte, header func(string) string) (WebhookCandidate, bool) {
	var found []WebhookCandidate
	for _, c := range e.Candidates {
		if c.Signed(body, header) {
			found = append(found, c)
		}
	}
	if len(found) != 1 {
		return WebhookCandidate{}, false
	}
	return found[0], true
}

// PlatformForWebhook resolves the platform that sent a webhook for the
// datasource. With one rule it is that rule's platform. With rules on several
// platforms, the request says which: the event header of the platform. Gitea
// also sends GitHub's header, so a platform of another type than GitHub wins
// when both fit.
func (s *Bundle) PlatformForWebhook(ctx context.Context, datasourceName string, header func(string) string) (Platform, string, error) {
	rules := s.RulesForDatasource(datasourceName)
	if len(rules) == 0 {
		return nil, "", fmt.Errorf("no rule found for datasource %q", datasourceName)
	}
	if len(rules) == 1 {
		plat, err := s.GetPlatformForRule(ctx, rules[0].Platform, datasourceName)
		return plat, rules[0].Platform, err
	}

	type candidate struct {
		name     string
		driver   string
		platform Platform
	}
	var fits []candidate
	expected := map[string]string{}
	for _, r := range rules {
		plat, err := s.GetPlatformForRule(ctx, r.Platform, datasourceName)
		if err != nil {
			return nil, "", err
		}
		expected[r.Platform] = plat.EventTypeHeader()
		if header(plat.EventTypeHeader()) != "" {
			fits = append(fits, candidate{r.Platform, s.platformDriver(r.Platform), plat})
		}
	}
	var specific []candidate
	for _, c := range fits {
		if c.driver != "github" {
			specific = append(specific, c)
		}
	}
	if len(specific) > 0 {
		fits = specific
	}
	switch len(fits) {
	case 0:
		return nil, "", &PlatformMismatchError{Datasource: datasourceName, Expected: expected}
	case 1:
		return fits[0].platform, fits[0].name, nil
	}
	names := make([]string, len(fits))
	candidates := make([]WebhookCandidate, len(fits))
	for i, c := range fits {
		names[i] = c.name
		candidates[i] = WebhookCandidate{Name: c.name, Platform: c.platform}
	}
	return nil, "", &AmbiguousPlatformError{Datasource: datasourceName, Platforms: names, Candidates: candidates}
}

// platformDriver is the driver type ("github", "gitea") of the named platform.
func (s *Bundle) platformDriver(name string) string {
	for _, p := range s.bundle.Platforms {
		if PlatformName(p) == name {
			return PlatformDriverName(p)
		}
	}
	return ""
}

// defaultSecretManagerName is the secret manager a rule without its own
// secret_manager uses: the one tagged "default", else the bundle's only one.
func (s *Bundle) defaultSecretManagerName() (string, error) {
	var names []string
	for _, sm := range s.bundle.GetSecretManagers() {
		if n := SecretManagerName(sm); n != "" {
			names = append(names, n)
		}
	}
	for _, n := range names {
		if n == "vault-default" {
			return n, nil
		}
	}
	switch len(names) {
	case 1:
		return names[0], nil
	case 0:
		return "", fmt.Errorf("the bundle has no secret_managers to read a {ref} from")
	}
	return "", fmt.Errorf("the bundle has several secret managers (%s); name one with secret_manager on the rule, or tag one \"default\"", strings.Join(names, ", "))
}
