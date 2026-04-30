// zookeeper.go — ZooKeeper drift watcher.
//
// VarTrack writes config values as leaf znodes. Path order is controlled by
// zk_path_order ("tenant_env" default, or "env_tenant"):
//
//	/{tenant}/{env}/{config_key}               (zk_path_order = "tenant_env", default)
//	/{env}/{tenant}/{config_key}               (zk_path_order = "env_tenant")
//	/{base_path}/{tenant}/{env}/{config_key}   (with optional zk_base_path prefix)
//
// and a metadata znode alongside each env subtree:
//
//	/{tenant}/{env}/__vartrack__
//	  ← {"app.kubernetes.io/managed-by": "vartrack", "vartrack.io/tenant": "...", ...}
//
// The watcher:
//  1. Lists all env subtrees that contain a __vartrack__ metadata znode
//     (meaning VarTrack manages them).
//  2. For each managed subtree, reads all leaf znode values.
//  3. Computes a SHA-256 fingerprint.
//  4. On drift → Restore() → orchestrator re-sync.
//
// ZooKeeper native watches are set on each __vartrack__ metadata znode so
// that the watcher is notified immediately on change — no need to wait for
// the next poll tick.  The poll tick is still kept as a safety net.
package watcher

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"strings"
	"time"

	"github.com/go-zookeeper/zk"

	"watcher-service/internal/config"
	dsv1 "watcher-service/internal/gen/proto/go/vartrack/v1/models/datasources"
	models "watcher-service/internal/gen/proto/go/vartrack/v1/models"
	"watcher-service/internal/healer"
)

// ZKWatcher watches a ZooKeeper tree for drift in VarTrack-managed znodes.
type ZKWatcher struct {
	name     string
	conn     *zk.Conn
	basePath string
	healer   *healer.Healer
	healOpts healer.HealRequest

	// eventCh receives native ZooKeeper watch events for immediate detection.
	eventCh <-chan zk.Event
}

// NewZKWatcher connects to ZooKeeper and returns a ready ZKWatcher.
func NewZKWatcher(
	_ context.Context,
	cfg *dsv1.ZooKeeperConfig,
	rule *models.Rule,
	h *healer.Healer,
) (*ZKWatcher, error) {
	if len(cfg.GetHosts()) == 0 {
		return nil, fmt.Errorf("zk watcher %s: no addresses configured", config.RuleName(rule))
	}

	sessionTimeout := 10 * time.Second
	if d := cfg.GetSessionTimeout(); d != nil {
		sessionTimeout = d.AsDuration()
	}

	conn, eventCh, err := zk.Connect(cfg.GetHosts(), sessionTimeout, zk.WithLogInfo(false))
	if err != nil {
		return nil, fmt.Errorf("zk watcher %s: connect %v: %w", config.RuleName(rule), cfg.GetHosts(), err)
	}

	basePath := cfg.GetBasePath()

	slog.Info("zk watcher: connected",
		"watcher", config.RuleName(rule), "addrs", cfg.GetHosts(), "base_path", basePath)

	return &ZKWatcher{
		name:     config.RuleName(rule),
		conn:     conn,
		basePath: basePath,
		healer:   h,
		healOpts: healer.HealRequest{
			Datasource: rule.GetDatasource(),
			Platform:   rule.GetPlatform(),
		},
		eventCh: eventCh,
	}, nil
}

// Name implements Watcher.
func (w *ZKWatcher) Name() string { return "zookeeper/" + w.name }

// Snapshot reads all VarTrack-managed znode values and returns a fingerprint.
//
// Walk order:
//  1. List children of basePath → tenants
//  2. List children of basePath/{tenant} → envs
//  3. For each env: check for __vartrack__ meta znode → if present, it is managed.
//  4. Collect all leaf znode data values (non-meta znodes).
func (w *ZKWatcher) Snapshot(ctx context.Context) (string, error) {
	records := make(map[string]string)
	if err := w.walkManaged(ctx, w.basePath, records); err != nil {
		return "", fmt.Errorf("zk snapshot %s: %w", w.name, err)
	}
	return FingerprintRecords(records), nil
}

// Restore implements Watcher.
func (w *ZKWatcher) Restore(ctx context.Context) error {
	slog.Info("zk watcher: triggering heal", "watcher", w.name)
	return w.healer.Heal(ctx, w.healOpts)
}

// Close closes the ZooKeeper connection.
func (w *ZKWatcher) Close() error {
	w.conn.Close()
	return nil
}

// WatchEvents returns a channel that fires on native ZK events.
// The manager can use this to detect drift before the next poll tick.
func (w *ZKWatcher) WatchEvents() <-chan zk.Event { return w.eventCh }

// ─── internal ─────────────────────────────────────────────────────────────────

// walkManaged recursively walks the ZooKeeper tree under root and fills
// records with all leaf values under VarTrack-managed env subtrees.
func (w *ZKWatcher) walkManaged(ctx context.Context, root string, records map[string]string) error {
	select {
	case <-ctx.Done():
		return ctx.Err()
	default:
	}

	children, _, err := w.conn.Children(root)
	if err != nil {
		if err == zk.ErrNoNode {
			return nil // path doesn't exist yet
		}
		return fmt.Errorf("children %s: %w", root, err)
	}

	for _, child := range children {
		childPath := joinPath(root, child)

		// Leaf: if this znode has data, it may be a config value.
		data, _, err := w.conn.Get(childPath)
		if err != nil {
			slog.Warn("zk watcher: get failed", "path", childPath, "error", err)
			continue
		}

		// Check if this path level contains a __vartrack__ meta znode.
		if child == ZKMetaZnode {
			// Parse the metadata to confirm VarTrack manages this subtree.
			if !isVarTrackMeta(data) {
				return nil // not managed by vartrack — skip
			}
			// Meta znode itself is not a config value; set a watch for changes.
			w.setWatch(childPath)
			continue
		}

		// Recurse into subtrees.
		subChildren, _, _ := w.conn.Children(childPath)
		if len(subChildren) > 0 {
			if err := w.walkManaged(ctx, childPath, records); err != nil {
				return err
			}
			continue
		}

		// True leaf node (no children) — include in fingerprint.
		// Use the path relative to basePath as the key.
		key := strings.TrimPrefix(childPath, w.basePath+"/")
		records[key] = string(data)
	}
	return nil
}

// isVarTrackMeta returns true if the raw bytes from a __vartrack__ znode
// contain the "app.kubernetes.io/managed-by": "vartrack" label.
func isVarTrackMeta(data []byte) bool {
	if len(data) == 0 {
		return false
	}
	var meta map[string]string
	if err := json.Unmarshal(data, &meta); err != nil {
		return false
	}
	return meta["app.kubernetes.io/managed-by"] == ManagedByValue
}

// setWatch puts a one-shot ZooKeeper data watch on path.
// The watch fires on the global eventCh — the manager can react immediately.
func (w *ZKWatcher) setWatch(path string) {
	_, _, _, err := w.conn.GetW(path)
	if err != nil {
		slog.Debug("zk watcher: set watch failed", "path", path, "error", err)
	}
}

// joinPath builds a ZooKeeper path avoiding double slashes.
func joinPath(parent, child string) string {
	if strings.HasSuffix(parent, "/") {
		return parent + child
	}
	return parent + "/" + child
}
