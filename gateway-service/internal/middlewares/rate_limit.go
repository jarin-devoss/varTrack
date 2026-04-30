package middlewares

import (
	"fmt"
	"log/slog"
	"math"
	"net/http"
	"strconv"
	"sync"
	"time"

	"golang.org/x/time/rate"
)

// RateLimiterConfig configures the global rate limiter.
type RateLimiterConfig struct {
	// Global controls.
	Rate  rate.Limit // Global token-bucket refill rate (requests/sec).
	Burst int        // Maximum burst size (bucket capacity).

	// Per-IP exponential back-off.
	PerIPRate            rate.Limit    // Base per-IP limit.
	PerIPBurst           int           // Per-IP bucket capacity.
	MaxBackoffMultiplier float64       // Cap for exponential multiplier.
	BackoffDecayInterval time.Duration // How long until a violation "heals".
	IPCleanupInterval    time.Duration // How often to reap idle IPs.
}

// DefaultRateLimiterConfig returns production-ready defaults.
func DefaultRateLimiterConfig() RateLimiterConfig {
	return RateLimiterConfig{
		Rate:                 100,
		Burst:                200,
		PerIPRate:            20,
		PerIPBurst:           40,
		MaxBackoffMultiplier: 8,
		BackoffDecayInterval: 30 * time.Second,
		IPCleanupInterval:    10 * time.Minute,
	}
}

// RateLimiter combines a global token bucket with per-IP exponential
// back-off to prevent abuse while ensuring fair access.
type RateLimiter struct {
	global     *rate.Limiter
	ipLimiters sync.Map // string -> *ipState
	config     RateLimiterConfig
}

type ipState struct {
	mu                sync.Mutex
	limiter           *rate.Limiter
	violations        int
	lastViolationTime time.Time
	blockedUntil      time.Time // Fixed typo: was "blockedUtil"
	lastSeen          time.Time
}

// NewRateLimiter creates a new RateLimiter and starts its cleanup goroutine.
func NewRateLimiter(cfg RateLimiterConfig) *RateLimiter {
	rl := &RateLimiter{
		global: rate.NewLimiter(cfg.Rate, cfg.Burst),
		config: cfg,
	}
	go rl.cleanup()
	return rl
}

// Middleware wraps an http.Handler with rate limiting and adds X-RateLimit headers.
func (rl *RateLimiter) Middleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		ip := extractIP(r)
		state := rl.getOrCreateIPState(ip)

		now := time.Now()

		state.mu.Lock()
		state.lastSeen = now

		if now.Before(state.blockedUntil) {
			blockedUntil := state.blockedUntil
			state.mu.Unlock()

			slog.Warn("IP blocked by exponential back-off",
				"ip", ip,
				"blocked_until", blockedUntil.Format(time.RFC3339),
				"correlation_id", GetCorrelationID(r.Context()),
			)
			w.Header().Set("Retry-After", fmt.Sprintf("%.0f", time.Until(blockedUntil).Seconds()))
			rl.writeRateLimitHeaders(w, state.limiter, ip)
			http.Error(w, http.StatusText(http.StatusTooManyRequests), http.StatusTooManyRequests)
			return
		}

		// Decay violations over time.
		if !state.lastViolationTime.IsZero() && now.Sub(state.lastViolationTime) > rl.config.BackoffDecayInterval {
			if state.violations > 0 {
				state.violations--
			}
			state.lastViolationTime = now
		}
		state.mu.Unlock()

		// Global rate limit check (performed AFTER back-off check so blocked IPs don't drain global tokens).
		if !rl.global.Allow() {
			slog.Warn("global rate limit exceeded",
				"correlation_id", GetCorrelationID(r.Context()),
			)
			rl.writeRateLimitHeaders(w, rl.global, "global")
			http.Error(w, http.StatusText(http.StatusTooManyRequests), http.StatusTooManyRequests)
			return
		}

		// Per-IP allow check.
		if !state.limiter.Allow() {
			state.mu.Lock()
			// Debounce: concurrent requests that all fail Allow() simultaneously
			// queue at this lock.  The first goroutine sets blockedUntil; the
			// rest must not pile on additional violations for the same event.
			if now.Before(state.blockedUntil) {
				blockedUntil := state.blockedUntil
				state.mu.Unlock()
				slog.Warn("IP blocked by exponential back-off (concurrent)",
					"ip", ip,
					"blocked_until", blockedUntil.Format(time.RFC3339),
					"correlation_id", GetCorrelationID(r.Context()),
				)
				w.Header().Set("Retry-After", fmt.Sprintf("%.0f", time.Until(blockedUntil).Seconds()))
				rl.writeRateLimitHeaders(w, state.limiter, ip)
				http.Error(w, http.StatusText(http.StatusTooManyRequests), http.StatusTooManyRequests)
				return
			}
			state.violations++
			if state.violations > 10 {
				state.violations = 10
			}
			state.lastViolationTime = now

			multiplier := math.Pow(2, float64(state.violations))
			if multiplier > rl.config.MaxBackoffMultiplier {
				multiplier = rl.config.MaxBackoffMultiplier
			}
			backoff := time.Duration(multiplier) * time.Second
			state.blockedUntil = now.Add(backoff)
			violations := state.violations
			state.mu.Unlock()

			slog.Warn("per-IP rate limit exceeded",
				"ip", ip,
				"violations", violations,
				"backoff", backoff.String(),
				"correlation_id", GetCorrelationID(r.Context()),
			)
			w.Header().Set("Retry-After", fmt.Sprintf("%.0f", backoff.Seconds()))
			rl.writeRateLimitHeaders(w, state.limiter, ip)
			http.Error(w, http.StatusText(http.StatusTooManyRequests), http.StatusTooManyRequests)
			return
		}

		rl.writeRateLimitHeaders(w, state.limiter, ip)
		next.ServeHTTP(w, r)
	})
}

// writeRateLimitHeaders adds X-RateLimit-* headers to the response.
func (rl *RateLimiter) writeRateLimitHeaders(w http.ResponseWriter, limiter *rate.Limiter, scope string) {
	w.Header().Set("X-RateLimit-Limit", strconv.Itoa(limiter.Burst()))
	w.Header().Set("X-RateLimit-Remaining", strconv.Itoa(int(limiter.Tokens())))
	w.Header().Set("X-RateLimit-Scope", scope)
}

// getOrCreateIPState returns the per-IP state, creating it if necessary.
func (rl *RateLimiter) getOrCreateIPState(ip string) *ipState {
	if val, ok := rl.ipLimiters.Load(ip); ok {
		return val.(*ipState)
	}

	state := &ipState{
		limiter:  rate.NewLimiter(rl.config.PerIPRate, rl.config.PerIPBurst),
		lastSeen: time.Now(),
	}
	val, _ := rl.ipLimiters.LoadOrStore(ip, state)
	return val.(*ipState)
}

// cleanup removes stale IP entries to bound memory usage.
func (rl *RateLimiter) cleanup() {
	ticker := time.NewTicker(rl.config.IPCleanupInterval)
	defer ticker.Stop()

	for range ticker.C {
		now := time.Now()
		reaped := 0
		remaining := 0

		rl.ipLimiters.Range(func(key, value any) bool {
			state := value.(*ipState)
			state.mu.Lock()
			// An IP is idle if it has no tracked violations, is no longer blocked, and hasn't sent a request recently.
			isIdle := state.violations == 0 && now.After(state.blockedUntil) && now.Sub(state.lastSeen) > rl.config.IPCleanupInterval

			if isIdle {
				rl.ipLimiters.Delete(key)
				reaped++
			} else {
				remaining++
			}
			state.mu.Unlock()
			return true
		})

		if reaped > 0 {
			slog.Debug("rate limiter cleanup", "reaped", reaped, "remaining", remaining)
		}
	}
}
