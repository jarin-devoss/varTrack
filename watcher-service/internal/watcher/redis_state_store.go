// redis_state_store.go — Redis-backed implementation of StateBackend.
//
// Uses hash key "vtwatch:state" where each field is a watcher name and the
// value is the fingerprint.  This lets all watchers share a single Redis key
// and makes HSET/HGET the cheapest possible operations.
//
// Falls back to "" (missing) gracefully — the manager will take a fresh
// snapshot and establish a new baseline if Redis returns an error.
package watcher

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"github.com/redis/go-redis/v9"
)

const (
	_redisStateKey = "vtwatch:state"
	_redisTTL      = 7 * 24 * time.Hour // 7 days — survives restarts
)

// RedisStateStore implements StateBackend using Redis hashes.
type RedisStateStore struct {
	client *redis.Client
}

// NewRedisStateStore connects to the given Redis URL and returns a
// *RedisStateStore.  Returns an error if the connection ping fails.
func NewRedisStateStore(redisURL string) (*RedisStateStore, error) {
	opts, err := redis.ParseURL(redisURL)
	if err != nil {
		return nil, fmt.Errorf("redis_state_store: parse URL: %w", err)
	}
	// Use a dedicated DB (3) to avoid collision with the orchestrator cache (2)
	// and Celery broker (0) / result backend (1).
	opts.DB = 3
	opts.DialTimeout = 5 * time.Second
	opts.ReadTimeout = 2 * time.Second
	opts.WriteTimeout = 2 * time.Second

	client := redis.NewClient(opts)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := client.Ping(ctx).Err(); err != nil {
		_ = client.Close()
		return nil, fmt.Errorf("redis_state_store: ping failed: %w", err)
	}
	slog.Info("watcher: Redis state store connected", "url", redisURL, "db", 3)
	return &RedisStateStore{client: client}, nil
}

// Load returns the persisted fingerprint for key, or "" on miss / error.
func (r *RedisStateStore) Load(key string) string {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()
	val, err := r.client.HGet(ctx, _redisStateKey, key).Result()
	if err != nil {
		// redis.Nil means "key doesn't exist" — normal on first run.
		if err != redis.Nil {
			slog.Debug("redis_state_store: load error", "key", key, "err", err)
		}
		return ""
	}
	return val
}

// Save persists a fingerprint for key and resets the TTL on the hash.
func (r *RedisStateStore) Save(key, fingerprint string) error {
	ctx, cancel := context.WithTimeout(context.Background(), 2*time.Second)
	defer cancel()

	pipe := r.client.Pipeline()
	pipe.HSet(ctx, _redisStateKey, key, fingerprint)
	pipe.Expire(ctx, _redisStateKey, _redisTTL)
	_, err := pipe.Exec(ctx)
	if err != nil {
		return fmt.Errorf("redis_state_store: save %s: %w", key, err)
	}
	return nil
}
