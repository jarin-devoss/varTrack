// redis_elector.go — Redis-based leader election for the watcher-service.
//
// Uses the SET NX PX distributed lock pattern:
//
//  1. Try SET vtwatch:leader <instance-id> NX PX <ttl-ms>
//  2. If acquired → this instance is the leader.
//  3. Leader renews the lock every ttl/3 (heartbeat) to prevent expiry.
//  4. If renewal fails (Redis down, key gone) → leadership lost, fn context cancelled.
//  5. Non-leaders poll every retryInterval until they acquire the lock.
//
// Unlike ZooKeeper, Redis does not push notifications, so non-leaders poll.
// The poll interval is set to ttl/3 so that a slot opens within one TTL
// after the leader crashes.
package election

import (
	"context"
	"fmt"
	"log/slog"
	"os"
	"time"

	"github.com/redis/go-redis/v9"
)

const (
	_redisLockKey  = "vtwatch:leader"
	_redisLockDB   = 4 // distinct from state store (3), celery (0/1), cache (2)
	_lockTTL       = 15 * time.Second
	_heartbeatTick = _lockTTL / 3 // renew well before expiry
	_pollInterval  = _lockTTL / 3 // non-leader poll cadence
)

// RedisElector implements leader election using a Redis SET NX PX lock.
type RedisElector struct {
	client     *redis.Client
	instanceID string
}

// NewRedisElector connects to Redis and returns a RedisElector.
// redisURL follows the standard Redis URL format (redis://[:password@]host:port).
// instanceID uniquely identifies this replica; defaults to the hostname when empty.
func NewRedisElector(redisURL, instanceID string) (*RedisElector, error) {
	opts, err := redis.ParseURL(redisURL)
	if err != nil {
		return nil, fmt.Errorf("redis elector: parse URL: %w", err)
	}
	opts.DB = _redisLockDB
	opts.DialTimeout = 5 * time.Second
	opts.ReadTimeout = 2 * time.Second
	opts.WriteTimeout = 2 * time.Second

	client := redis.NewClient(opts)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := client.Ping(ctx).Err(); err != nil {
		_ = client.Close()
		return nil, fmt.Errorf("redis elector: ping failed: %w", err)
	}

	if instanceID == "" {
		instanceID, _ = os.Hostname()
	}

	slog.Info("election: Redis elector ready", "instance_id", instanceID, "db", _redisLockDB)
	return &RedisElector{client: client, instanceID: instanceID}, nil
}

// Run participates in leader election until ctx is cancelled.
//
// When this instance acquires the Redis lock it calls fn with a derived
// context. That context is cancelled when the lock cannot be renewed
// (Redis unavailable, key deleted, TTL expired). Run then re-enters the
// poll loop to acquire the lock again.
func (e *RedisElector) Run(ctx context.Context, fn func(ctx context.Context)) error {
	for {
		if ctx.Err() != nil {
			return nil
		}

		if e.tryAcquire(ctx) {
			slog.Info("election: Redis lock acquired — became leader", "instance", e.instanceID)

			leaderCtx, leaderCancel := context.WithCancel(ctx)
			done := make(chan struct{})

			go func() {
				defer close(done)
				fn(leaderCtx)
			}()

			e.heartbeat(leaderCtx, leaderCancel)
			leaderCancel()
			<-done

			// Release the lock so the next replica can acquire it immediately
			// instead of waiting for TTL expiry.
			e.release(ctx)
			slog.Info("election: Redis lock released", "instance", e.instanceID)
		} else {
			slog.Debug("election: Redis lock held by another instance — polling",
				"poll_interval", _pollInterval)
		}

		select {
		case <-ctx.Done():
			return nil
		case <-time.After(_pollInterval):
		}
	}
}

// Close closes the underlying Redis client.
func (e *RedisElector) Close() {
	_ = e.client.Close()
}

// ─── internal ─────────────────────────────────────────────────────────────────

// tryAcquire attempts a single SET NX PX acquisition.
func (e *RedisElector) tryAcquire(ctx context.Context) bool {
	tctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()
	ok, err := e.client.SetNX(tctx, _redisLockKey, e.instanceID, _lockTTL).Result()
	if err != nil {
		slog.Warn("election: Redis SetNX error", "error", err)
		return false
	}
	return ok
}

// heartbeat renews the lock every _heartbeatTick until the lock can no longer
// be renewed or leaderCtx is cancelled. On renewal failure it cancels
// leaderCtx to stop the manager.
func (e *RedisElector) heartbeat(leaderCtx context.Context, cancel context.CancelFunc) {
	ticker := time.NewTicker(_heartbeatTick)
	defer ticker.Stop()

	for {
		select {
		case <-leaderCtx.Done():
			return
		case <-ticker.C:
			if !e.renew(leaderCtx) {
				slog.Error("election: Redis lock renewal failed — releasing leadership",
					"instance", e.instanceID)
				cancel()
				return
			}
			slog.Debug("election: Redis lock renewed", "instance", e.instanceID)
		}
	}
}

// renew uses GETEX to extend the TTL only if this instance still owns the lock.
func (e *RedisElector) renew(ctx context.Context) bool {
	tctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()

	// Lua script: extend only if we still own the key (atomic check-and-expire).
	script := redis.NewScript(`
		if redis.call("GET", KEYS[1]) == ARGV[1] then
			return redis.call("PEXPIRE", KEYS[1], ARGV[2])
		else
			return 0
		end
	`)

	ttlMS := fmt.Sprintf("%d", _lockTTL.Milliseconds())
	result, err := script.Run(tctx, e.client, []string{_redisLockKey}, e.instanceID, ttlMS).Int()
	if err != nil {
		slog.Warn("election: Redis renewal script error", "error", err)
		return false
	}
	return result == 1
}

// release deletes the lock key only if this instance owns it.
func (e *RedisElector) release(ctx context.Context) {
	tctx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()

	script := redis.NewScript(`
		if redis.call("GET", KEYS[1]) == ARGV[1] then
			return redis.call("DEL", KEYS[1])
		else
			return 0
		end
	`)
	_, _ = script.Run(tctx, e.client, []string{_redisLockKey}, e.instanceID).Result()
}
