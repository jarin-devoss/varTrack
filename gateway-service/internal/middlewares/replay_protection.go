package middlewares

import (
	"fmt"
	"net/http"
	"strconv"
	"sync"
	"time"
)

// ReplayProtectionConfig configures the replay protection window.
type ReplayProtectionConfig struct {
	Window      time.Duration // Maximum timestamp age allowed.
	FutureDrift time.Duration // Maximum clock drift into the future.
}

// DefaultReplayProtectionConfig returns production-ready defaults.
func DefaultReplayProtectionConfig() ReplayProtectionConfig {
	return ReplayProtectionConfig{
		Window:      5 * time.Minute,
		FutureDrift: 30 * time.Second,
	}
}

// NonceStore is the persistence backend for delivery nonces.
// Implemented by memoryNonceStore (single process) and RedisNonceStore
// (distributed — required for multi-replica deployments).
type NonceStore interface {
	// CheckAndSet returns true and records the nonce if it is new within the
	// window.  Returns false without storing if the nonce was already seen.
	CheckAndSet(nonce string, window time.Duration) bool
}

// memoryNonceStore is the default in-process nonce store.
type memoryNonceStore struct {
	mu         sync.Mutex
	seenNonces map[string]time.Time
}

func newMemoryNonceStore() *memoryNonceStore {
	return &memoryNonceStore{seenNonces: make(map[string]time.Time)}
}

func (m *memoryNonceStore) CheckAndSet(nonce string, window time.Duration) bool {
	m.mu.Lock()
	defer m.mu.Unlock()

	now := time.Now()
	// Prune expired nonces to bound memory growth.
	for k, t := range m.seenNonces {
		if now.Sub(t) > window {
			delete(m.seenNonces, k)
		}
	}
	if _, exists := m.seenNonces[nonce]; exists {
		return false
	}
	m.seenNonces[nonce] = now
	return true
}

// ReplayProtector validates event timestamps and tracks delivery nonces to
// mitigate replay attacks.
type ReplayProtector struct {
	config ReplayProtectionConfig
	store  NonceStore
}

// NewReplayProtector creates a new ReplayProtector backed by an in-process
// memory store.  Use NewReplayProtectorWithStore to inject a Redis store for
// multi-replica deployments.
func NewReplayProtector(cfg ReplayProtectionConfig) *ReplayProtector {
	return &ReplayProtector{config: cfg, store: newMemoryNonceStore()}
}

// NewReplayProtectorWithStore creates a ReplayProtector that delegates nonce
// persistence to the provided NonceStore.  Use this with RedisNonceStore for
// multi-replica gateway deployments where each instance must share the nonce
// window to block cross-replica replay attacks.
func NewReplayProtectorWithStore(cfg ReplayProtectionConfig, store NonceStore) *ReplayProtector {
	return &ReplayProtector{config: cfg, store: store}
}

// CheckNonce verifies that *nonce* has not been seen within the replay window.
// Returns false (replay detected) if the nonce was seen before.
func (rp *ReplayProtector) CheckNonce(nonce string) bool {
	if nonce == "" {
		return true // no nonce — skip nonce check
	}
	return rp.store.CheckAndSet(nonce, rp.config.Window)
}

// Validate checks that the event time is within the acceptable window.
func (rp *ReplayProtector) Validate(eventTime time.Time) error {
	if eventTime.IsZero() {
		return nil // No timestamp extracted — skip validation.
	}

	now := time.Now()
	age := now.Sub(eventTime)

	if age > rp.config.Window {
		return fmt.Errorf("event timestamp too old: age=%s, max=%s", age, rp.config.Window)
	}
	if age < -rp.config.FutureDrift {
		return fmt.Errorf("event timestamp in the future: drift=%s, max=%s", -age, rp.config.FutureDrift)
	}

	return nil
}

// TimestampExtractor extracts an event timestamp from an HTTP request.
// Returns zero time if extraction is not possible (validation is skipped).
type TimestampExtractor func(r *http.Request) (time.Time, error)

// ExtractGitHubTimestamp is the default extractor. GitHub webhook payloads
// don't include a timestamp header, so validation is skipped.
func ExtractGitHubTimestamp(_ *http.Request) (time.Time, error) {
	return time.Time{}, nil
}

// ExtractSlackTimestamp reads the X-Slack-Request-Timestamp header.
func ExtractSlackTimestamp(r *http.Request) (time.Time, error) {
	ts := r.Header.Get("X-Slack-Request-Timestamp")
	if ts == "" {
		return time.Time{}, nil
	}
	unixSec, err := strconv.ParseInt(ts, 10, 64)
	if err != nil {
		return time.Time{}, fmt.Errorf("invalid X-Slack-Request-Timestamp: %w", err)
	}
	return time.Unix(unixSec, 0), nil
}

// ExtractStripeTimestamp reads the t= field from the Stripe-Signature header.
func ExtractStripeTimestamp(r *http.Request) (time.Time, error) {
	sig := r.Header.Get("Stripe-Signature")
	if sig == "" {
		return time.Time{}, nil
	}

	// Parse "t=<unix>,v1=<sig>" format.
	for _, part := range splitOnComma(sig) {
		if len(part) > 2 && part[0] == 't' && part[1] == '=' {
			unixSec, err := strconv.ParseInt(part[2:], 10, 64)
			if err != nil {
				return time.Time{}, fmt.Errorf("invalid stripe timestamp: %w", err)
			}
			return time.Unix(unixSec, 0), nil
		}
	}
	return time.Time{}, nil
}

// splitOnComma splits a string by comma without allocating a []string
// for the common single-element case.
func splitOnComma(s string) []string {
	parts := make([]string, 0, 4)
	start := 0
	for i := 0; i < len(s); i++ {
		if s[i] == ',' {
			parts = append(parts, s[start:i])
			start = i + 1
		}
	}
	parts = append(parts, s[start:])
	return parts
}
