package middlewares

import (
	"fmt"
	"log/slog"
	"net"
	"net/http"
	"sync"
	"time"

	"golang.org/x/time/rate"
)

// KeyedRateLimiterConfig configures per-key rate limiting behaviour.
type KeyedRateLimiterConfig struct {
	Rate            rate.Limit    // Per-key requests per second.
	Burst           int           // Maximum burst size per key.
	CleanupInterval time.Duration // How often to reap idle keys.
	MaxIdleAge      time.Duration // Keys idle longer than this are reaped.
}

// DefaultKeyedRateLimiterConfig returns production-ready defaults.
func DefaultKeyedRateLimiterConfig() KeyedRateLimiterConfig {
	return KeyedRateLimiterConfig{
		Rate:            10,
		Burst:           20,
		CleanupInterval: 5 * time.Minute,
		MaxIdleAge:      10 * time.Minute,
	}
}

// KeyedRateLimiter enforces per-key rate limits to prevent one noisy
// datasource or IP from starving others.
type KeyedRateLimiter struct {
	limiters        sync.Map // string -> *keyedEntry
	rate            rate.Limit
	burst           int
	cleanupInterval time.Duration
	maxIdleAge      time.Duration
}

type keyedEntry struct {
	mu       sync.Mutex
	limiter  *rate.Limiter
	lastSeen time.Time
}

// NewKeyedRateLimiter creates a new keyed rate limiter and starts its reaper.
func NewKeyedRateLimiter(cfg KeyedRateLimiterConfig) *KeyedRateLimiter {
	krl := &KeyedRateLimiter{
		rate:            cfg.Rate,
		burst:           cfg.Burst,
		cleanupInterval: cfg.CleanupInterval,
		maxIdleAge:      cfg.MaxIdleAge,
	}
	go krl.reaper()
	return krl
}

// Middleware wraps an http.Handler with per-key rate limiting.
func (krl *KeyedRateLimiter) Middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		key := krl.extractKey(r)

		var entry *keyedEntry
		if val, ok := krl.limiters.Load(key); ok {
			entry = val.(*keyedEntry)
		} else {
			entry = &keyedEntry{
				limiter:  rate.NewLimiter(krl.rate, krl.burst),
				lastSeen: time.Now(),
			}
			val, _ := krl.limiters.LoadOrStore(key, entry)
			entry = val.(*keyedEntry)
		}

		entry.mu.Lock()
		entry.lastSeen = time.Now()
		limiter := entry.limiter
		entry.mu.Unlock()

		if !limiter.Allow() {
			slog.Warn("keyed rate limit exceeded",
				"key", key,
				"correlation_id", GetCorrelationID(r.Context()),
			)
			w.Header().Set("X-RateLimit-Key", key)
			w.Header().Set("Retry-After", "5")
			http.Error(w, http.StatusText(http.StatusTooManyRequests), http.StatusTooManyRequests)
			return
		}

		w.Header().Set("X-RateLimit-Key", key)
		next.ServeHTTP(w, r)
	})
}

// extractKey builds a rate-limit key from IP + datasource.
func (krl *KeyedRateLimiter) extractKey(r *http.Request) string {
	ip := extractIP(r)
	datasource := r.PathValue("datasource")
	if datasource == "" {
		datasource = "__default__"
	}
	return fmt.Sprintf("%s:%s", ip, datasource)
}

func extractIP(r *http.Request) string {
	host, _, err := net.SplitHostPort(r.RemoteAddr)
	if err != nil {
		return r.RemoteAddr
	}
	return host
}

// reaper periodically removes idle keys to prevent memory leaks.
func (krl *KeyedRateLimiter) reaper() {
	ticker := time.NewTicker(krl.cleanupInterval)
	defer ticker.Stop()

	for range ticker.C {
		now := time.Now()
		reaped := 0
		remaining := 0
		krl.limiters.Range(func(key, value any) bool {
			entry := value.(*keyedEntry)
			entry.mu.Lock()
			isIdle := now.Sub(entry.lastSeen) > krl.maxIdleAge
			entry.mu.Unlock()

			if isIdle {
				krl.limiters.Delete(key)
				reaped++
			} else {
				remaining++
			}
			return true
		})

		if reaped > 0 {
			slog.Debug("keyed rate limiter reaper ran",
				"reaped", reaped,
				"remaining", remaining,
			)
		}
	}
}
