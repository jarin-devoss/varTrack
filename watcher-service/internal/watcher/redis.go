// redis.go — Redis drift watcher.
//
// VarTrack writes config values under a namespace prefix:
//
//	{tenant_id}:{datasource}:{env}:{field}        ← config data
//	{tenant_id}:{datasource}:{env}:__vartrack__   ← meta hash (HSET)
//
// The watcher scans for all "__vartrack__" meta keys, confirms they are
// VarTrack-managed (_vt_managed_by = "vartrack"), then reads the sibling
// data keys and computes a SHA-256 fingerprint.
//
// Redis Cluster is supported automatically — the client routes by slot.
package watcher

import (
	"context"
	"fmt"
	"log/slog"
	"strings"

	"github.com/redis/go-redis/v9"

	"watcher-service/internal/config"
	dsv1 "watcher-service/internal/gen/proto/go/vartrack/v1/models/datasources"
	enums "watcher-service/internal/gen/proto/go/vartrack/v1/enums"
	models "watcher-service/internal/gen/proto/go/vartrack/v1/models"
	"watcher-service/internal/healer"
)

// RedisWatcher watches a Redis namespace for drift in VarTrack-managed keys.
type RedisWatcher struct {
	name     string
	client   redis.UniversalClient
	healer   *healer.Healer
	healOpts healer.HealRequest
}

// NewRedisWatcher connects to Redis (single-node or cluster) using the
// connection params from rule_config and returns a ready RedisWatcher.
//
func NewRedisWatcher(
	ctx context.Context,
	cfg *dsv1.RedisConfig,
	rule *models.Rule,
	h *healer.Healer,
) (*RedisWatcher, error) {
	hosts := cfg.GetHosts()
	if len(hosts) == 0 {
		hosts = []string{"localhost:6379"}
	}
	password := cfg.GetPassword().GetValue()

	var client redis.UniversalClient
	switch cfg.GetMode() {
	case enums.DeploymentMode_CLUSTER:
		client = redis.NewClusterClient(&redis.ClusterOptions{
			Addrs:    hosts,
			Password: password,
		})
	case enums.DeploymentMode_SENTINEL:
		client = redis.NewFailoverClient(&redis.FailoverOptions{
			MasterName:    cfg.GetSentinelMaster(),
			SentinelAddrs: hosts,
			Password:      password,
			DB:            int(cfg.GetDatabase()),
		})
	default: // STANDALONE or unspecified
		addr := hosts[0]
		if len(hosts) > 1 {
			// Multiple hosts without explicit mode → treat as cluster.
			client = redis.NewClusterClient(&redis.ClusterOptions{
				Addrs:    hosts,
				Password: password,
			})
			break
		}
		client = redis.NewClient(&redis.Options{
			Addr:     addr,
			Password: password,
			DB:       int(cfg.GetDatabase()),
		})
	}

	if err := client.Ping(ctx).Err(); err != nil {
		_ = client.Close()
		return nil, fmt.Errorf("redis watcher %s: ping %v: %w", config.RuleName(rule), hosts, err)
	}

	slog.Info("redis watcher: connected",
		"watcher", config.RuleName(rule), "hosts", hosts)

	return &RedisWatcher{
		name:   config.RuleName(rule),
		client: client,
		healer: h,
		healOpts: healer.HealRequest{
			Datasource: rule.GetDatasource(),
			Platform:   rule.GetPlatform(),
		},
	}, nil
}

// Name implements Watcher.
func (w *RedisWatcher) Name() string { return "redis/" + w.name }

// Snapshot scans for all __vartrack__ meta hashes, verifies ownership, then
// reads sibling data keys and computes a fingerprint.
func (w *RedisWatcher) Snapshot(ctx context.Context) (string, error) {
	records := make(map[string]string)

	// SCAN for all keys ending in :__vartrack__
	err := w.scanKeys(ctx, "*:"+VTManagedByField[1:]+"__", func(metaKey string) {
		// metaKey example: "acme:payments:pr-42:__vartrack__"
		meta, err := w.client.HGetAll(ctx, metaKey).Result()
		if err != nil || meta[VTManagedByField] != ManagedByValue {
			return // not managed by vartrack, or fetch error
		}

		// Strip the :__vartrack__ suffix to get the namespace prefix.
		prefix := strings.TrimSuffix(metaKey, ":"+ZKMetaZnode)

		// Scan all sibling keys under this prefix.
		w.scanKeys(ctx, prefix+":*", func(dataKey string) {
			if strings.HasSuffix(dataKey, ":"+ZKMetaZnode) {
				return // skip the meta key itself
			}
			val, err := w.client.Get(ctx, dataKey).Result()
			if err == nil {
				records[dataKey] = val
				return
			}
			// May be a hash key — try HGETALL.
			fields, err := w.client.HGetAll(ctx, dataKey).Result()
			if err == nil && len(fields) > 0 {
				for f, v := range fields {
					records[dataKey+":"+f] = v
				}
			}
		})
	})
	if err != nil {
		return "", fmt.Errorf("redis snapshot %s: scan: %w", w.name, err)
	}

	return FingerprintRecords(records), nil
}

// Restore implements Watcher.
func (w *RedisWatcher) Restore(ctx context.Context) error {
	slog.Info("redis watcher: triggering heal", "watcher", w.name)
	return w.healer.Heal(ctx, w.healOpts)
}

// Close disconnects from Redis.
func (w *RedisWatcher) Close() error { return w.client.Close() }

// ─── helper ───────────────────────────────────────────────────────────────────

// scanKeys runs SCAN with the given pattern and calls fn for each key.
// Works with both cluster (where SCAN must be called per-node) and single-node.
func (w *RedisWatcher) scanKeys(ctx context.Context, pattern string, fn func(string)) error {
	switch c := w.client.(type) {
	case *redis.ClusterClient:
		return c.ForEachMaster(ctx, func(ctx context.Context, node *redis.Client) error {
			return scanNode(ctx, node, pattern, fn)
		})
	default:
		return scanNode(ctx, w.client, pattern, fn)
	}
}

func scanNode(ctx context.Context, c redis.Cmdable, pattern string, fn func(string)) error {
	var cursor uint64
	for {
		keys, next, err := c.Scan(ctx, cursor, pattern, 100).Result()
		if err != nil {
			return err
		}
		for _, k := range keys {
			fn(k)
		}
		cursor = next
		if cursor == 0 {
			break
		}
	}
	return nil
}
