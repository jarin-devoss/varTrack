package secrets

import (
	"context"
	"log/slog"
	"sync"
	"time"

	pb_utils "gateway-service/internal/gen/proto/go/vartrack/v1/utils"
)

type cacheEntry struct {
	value     string
	expiresAt time.Time
}

// CacheConfig configures the TTL cache behaviour.
type CacheConfig struct {
	TTL             time.Duration // How long resolved secrets are cached. Default: 5m.
	CleanupInterval time.Duration // How often expired entries are reaped. Default: 10m.
}

// DefaultCacheConfig returns production-ready defaults.
func DefaultCacheConfig() CacheConfig {
	return CacheConfig{
		TTL:             5 * time.Minute,
		CleanupInterval: 10 * time.Minute,
	}
}

// CachingRefResolver wraps a RefResolver with a TTL cache
// to avoid hammering the secret manager on every webhook request.
//
// Two-layer defence:
//
//	Request → CachingRefResolver (TTL cache) →
//	            RefResolver (singleflight) →
//	              Secret Manager (one network call)
//
// Layer 1 (singleflight) collapses concurrent in-flight requests.
// Layer 2 (this TTL cache) short-circuits sequential bursts.
type CachingRefResolver struct {
	inner           *RefResolver
	mu              sync.RWMutex
	entries         map[string]*cacheEntry
	ttl             time.Duration
	cleanupInterval time.Duration
	closeCh         chan struct{}
}

// NewCachingRefResolver wraps resolver with a TTL cache.
func NewCachingRefResolver(resolver *RefResolver, cfg CacheConfig) *CachingRefResolver {
	if cfg.TTL <= 0 {
		cfg.TTL = DefaultCacheConfig().TTL
	}
	if cfg.CleanupInterval <= 0 {
		cfg.CleanupInterval = DefaultCacheConfig().CleanupInterval
	}

	c := &CachingRefResolver{
		inner:           resolver,
		entries:         make(map[string]*cacheEntry),
		ttl:             cfg.TTL,
		cleanupInterval: cfg.CleanupInterval,
		closeCh:         make(chan struct{}),
	}
	go c.reaper()
	return c
}

// Resolve returns the plain-text secret value, using the cache for external refs.
// Inline "value" secrets are passed through with zero overhead.
// Negative results (errors) are never cached.
func (c *CachingRefResolver) Resolve(ctx context.Context, ref *pb_utils.SecretRef, managerName string) (string, error) {
	if ref == nil {
		return "", nil
	}

	// Inline values: zero-cost pass-through.
	if _, ok := ref.Source.(*pb_utils.SecretRef_Value); ok {
		return c.inner.Resolve(ctx, ref, managerName)
	}

	key := c.cacheKey(ref, managerName)

	// Fast path — read lock only.
	c.mu.RLock()
	entry, ok := c.entries[key]
	c.mu.RUnlock()

	if ok && time.Now().Before(entry.expiresAt) {
		return entry.value, nil
	}

	// Slow path: resolve from singleflight → secret manager.
	value, err := c.inner.Resolve(ctx, ref, managerName)
	if err != nil {
		return "", err
	}

	c.mu.Lock()
	c.entries[key] = &cacheEntry{
		value:     value,
		expiresAt: time.Now().Add(c.ttl),
	}
	c.mu.Unlock()

	slog.Debug("secret resolved and cached", "manager", managerName, "ttl", c.ttl)
	return value, nil
}

// InvalidateAll clears all cached secrets.
func (c *CachingRefResolver) InvalidateAll() {
	c.mu.Lock()
	c.entries = make(map[string]*cacheEntry)
	c.mu.Unlock()
	slog.Info("secret cache: all entries invalidated")
}

// Invalidate removes a single cached entry.
func (c *CachingRefResolver) Invalidate(ref *pb_utils.SecretRef, managerName string) {
	if ref == nil {
		return
	}
	key := c.cacheKey(ref, managerName)
	c.mu.Lock()
	delete(c.entries, key)
	c.mu.Unlock()
}

// Len returns the current number of entries. Used for metrics.
func (c *CachingRefResolver) Len() int {
	c.mu.RLock()
	defer c.mu.RUnlock()
	return len(c.entries)
}

// Close stops the background reaper.
func (c *CachingRefResolver) Close() {
	close(c.closeCh)
}

// cacheKey builds a namespaced key. \x00 cannot appear in valid Vault paths.
func (c *CachingRefResolver) cacheKey(ref *pb_utils.SecretRef, managerName string) string {
	if extRef, ok := ref.Source.(*pb_utils.SecretRef_Ref); ok {
		return managerName + "\x00" + extRef.Ref.Path + "\x00" + extRef.Ref.Key
	}
	return managerName + "\x00__unknown__"
}

// reaper periodically removes expired entries to bound memory.
func (c *CachingRefResolver) reaper() {
	ticker := time.NewTicker(c.cleanupInterval)
	defer ticker.Stop()

	for {
		select {
		case <-c.closeCh:
			return
		case <-ticker.C:
			now := time.Now()
			c.mu.Lock()
			alive := make(map[string]*cacheEntry, len(c.entries))
			for k, entry := range c.entries {
				if !now.After(entry.expiresAt) {
					alive[k] = entry
				}
			}
			reaped := len(c.entries) - len(alive)
			remaining := len(alive)
			c.entries = alive
			c.mu.Unlock()

			if reaped > 0 {
				slog.Debug("secret cache reaper ran",
					"reaped", reaped,
					"remaining", remaining,
				)
			}
		}
	}
}
