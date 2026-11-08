package authz

// cache.go — what the Git platforms said, remembered for a short time.
//
// Every question the engine asks a platform ("who is this token", "what may it do
// in this repository", "may it push to this branch") costs a round trip, and a
// page of the web UI asks dozens. Answers are kept by (token, platform, question):
//
//   - the token is only ever stored as its SHA-256, never in the clear;
//   - concurrent askers of the same question share one round trip;
//   - only definite answers are kept: a network or server error never is, and a
//     rejected token only for a few seconds, which blunts repeated tries without
//     locking out someone who has just fixed their token;
//   - the cache is bounded, and drops the least recently used answer first.

import (
	"container/list"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"strings"
	"sync"
	"time"

	"golang.org/x/sync/singleflight"

	"gateway-service/internal/models"
)

const (
	// DefaultTTL is how long an answer is kept (AUTHZ_CACHE_TTL). It is also how
	// long a revoked permission keeps working.
	DefaultTTL = 60 * time.Second
	// rejectedTTL is how long "the platform rejected this token" is kept.
	rejectedTTL = 10 * time.Second
	// maxEntries bounds the cache.
	maxEntries = 10_000
)

type entry struct {
	key     string
	value   any
	err     error // nil, or models.ErrTokenRejected
	expires time.Time
}

type cache struct {
	ttl time.Duration
	now func() time.Time

	mu      sync.Mutex
	entries map[string]*list.Element // key → element of order, whose Value is *entry
	order   *list.List               // most recently used first
	flight  singleflight.Group
}

func newCache(ttl time.Duration) *cache {
	return &cache{ttl: ttl, now: time.Now, entries: map[string]*list.Element{}, order: list.New()}
}

// tokenKey names one token on one platform without holding the token.
func tokenKey(platform, token string) string {
	sum := sha256.Sum256([]byte(token))
	return hex.EncodeToString(sum[:]) + "|" + platform + "|"
}

// cached returns the answer to question for token on platform, asking fn only
// when no fresh answer is held.
func cached[T any](c *cache, platform, token, question string, fn func() (T, error)) (T, error) {
	if c == nil || c.ttl <= 0 {
		return fn()
	}
	key := tokenKey(platform, token) + question
	if v, err, ok := c.get(key); ok {
		if err != nil {
			var zero T
			return zero, err
		}
		return v.(T), nil
	}
	v, err, _ := c.flight.Do(key, func() (any, error) {
		// Another asker may have answered between this one's miss and its turn.
		if v, err, ok := c.get(key); ok {
			return v, err
		}
		value, err := fn()
		c.put(key, value, err)
		return value, err
	})
	if err != nil {
		var zero T
		return zero, err
	}
	return v.(T), nil
}

func (c *cache) get(key string) (any, error, bool) {
	c.mu.Lock()
	defer c.mu.Unlock()
	el, ok := c.entries[key]
	if !ok {
		return nil, nil, false
	}
	e := el.Value.(*entry)
	if !c.now().Before(e.expires) {
		c.order.Remove(el)
		delete(c.entries, key)
		return nil, nil, false
	}
	c.order.MoveToFront(el)
	return e.value, e.err, true
}

func (c *cache) put(key string, value any, err error) {
	ttl := c.ttl
	if err != nil {
		if !errors.Is(err, models.ErrTokenRejected) {
			return // not a definite answer
		}
		ttl = min(ttl, rejectedTTL)
		value = nil
	}
	c.mu.Lock()
	defer c.mu.Unlock()
	e := &entry{key: key, value: value, err: err, expires: c.now().Add(ttl)}
	if el, ok := c.entries[key]; ok {
		el.Value = e
		c.order.MoveToFront(el)
		return
	}
	c.entries[key] = c.order.PushFront(e)
	for c.order.Len() > maxEntries {
		oldest := c.order.Back()
		c.order.Remove(oldest)
		delete(c.entries, oldest.Value.(*entry).key)
	}
}

// forget drops every answer held for token on platform.
func (c *cache) forget(platform, token string) {
	if c == nil {
		return
	}
	prefix := tokenKey(platform, token)
	c.mu.Lock()
	defer c.mu.Unlock()
	for key, el := range c.entries {
		if strings.HasPrefix(key, prefix) {
			c.order.Remove(el)
			delete(c.entries, key)
		}
	}
}

// len is the number of answers held (for tests).
func (c *cache) len() int {
	c.mu.Lock()
	defer c.mu.Unlock()
	return len(c.entries)
}
