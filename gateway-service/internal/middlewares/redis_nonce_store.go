// redis_nonce_store.go — distributed Redis-backed NonceStore.
//
// Required for multi-replica gateway deployments.  Without a shared store,
// each replica maintains its own in-memory nonce set; an attacker can replay
// a captured GitHub webhook against a different replica and it will be accepted.
//
// Implementation:
//   - Uses Redis SET NX (set-if-not-exists) with an EX TTL equal to the
//     replay window.  Redis atomically handles the "is this nonce new?"
//     check and the "record it" write in a single round-trip.
//   - Falls back to accepting the request (true) if Redis is unreachable so
//     that a Redis outage does not block all webhook traffic.  The trade-off
//     is a brief period of degraded replay protection — acceptable because:
//     (a) replay attacks require capture + replay within the 5-minute window,
//     (b) the timestamp check still rejects stale payloads for platforms that
//         include one (Slack, Stripe).
package middlewares

import (
	"context"
	"log/slog"
	"time"

	"github.com/redis/go-redis/v9"
)

const _nonceKeyPrefix = "vtgw:nonce:"

// RedisNonceStore implements NonceStore using Redis SET NX + EX.
type RedisNonceStore struct {
	client *redis.Client
}

// NewRedisNonceStore connects to the given Redis URL and returns a
// *RedisNonceStore.  Returns an error if the connection ping fails.
func NewRedisNonceStore(redisURL string) (*RedisNonceStore, error) {
	opts, err := redis.ParseURL(redisURL)
	if err != nil {
		return nil, err
	}
	// Use DB 1 for nonce data, separate from any Celery or cache usage.
	opts.DB = 1
	opts.DialTimeout = 2 * time.Second
	opts.ReadTimeout = 1 * time.Second
	opts.WriteTimeout = 1 * time.Second

	client := redis.NewClient(opts)
	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()
	if err := client.Ping(ctx).Err(); err != nil {
		_ = client.Close()
		return nil, err
	}
	slog.Info("gateway: Redis nonce store connected", "db", 1)
	return &RedisNonceStore{client: client}, nil
}

// CheckAndSet atomically checks whether *nonce* is new and records it.
// Returns true (allow) when the nonce is new; false (block) when it's a replay.
// On Redis error, returns true and logs a warning to avoid blocking traffic.
func (r *RedisNonceStore) CheckAndSet(nonce string, window time.Duration) bool {
	ctx, cancel := context.WithTimeout(context.Background(), 1*time.Second)
	defer cancel()

	key := _nonceKeyPrefix + nonce
	// SET key 1 NX EX <seconds>:
	//   - NX: only set if key does not exist
	//   - Returns true (was set = new nonce) or false (already exists = replay)
	ok, err := r.client.SetNX(ctx, key, 1, window).Result()
	if err != nil {
		slog.Warn("gateway: Redis nonce store error — allowing request",
			"nonce", nonce, "error", err)
		return true // fail-open to avoid blocking all traffic
	}
	return ok
}

// Close releases the Redis connection.
func (r *RedisNonceStore) Close() error {
	return r.client.Close()
}
