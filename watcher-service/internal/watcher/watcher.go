// Package watcher defines the Watcher interface and shared helpers.
//
// Each watcher implementation (mongo, zookeeper, s3) polls a datasource for
// VarTrack-managed records, computes a fingerprint of the live state, and
// compares it against the last-known-good fingerprint.  Drift triggers a
// call to the Healer which asks the orchestrator to re-sync from git.
//
// The "desired" state is the fingerprint the watcher stored after the last
// successful sync.  On startup the watcher takes an initial snapshot —
// meaning any drift that occurred while the watcher was down is detected on
// the very first poll after the watcher restarts.
package watcher

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"sort"
	"time"
)

// Watcher polls a single datasource and reports drift.
type Watcher interface {
	// Name is the human-readable identifier, e.g. "mongo/acme-payments".
	Name() string

	// Snapshot computes a fingerprint (SHA-256 hex) of all VarTrack-managed
	// records currently in the datasource.
	Snapshot(ctx context.Context) (fingerprint string, err error)

	// Restore triggers the orchestrator to re-apply the desired state.
	// The Watcher implementation calls the Healer; the method is here so
	// that manager can orchestrate retries uniformly.
	Restore(ctx context.Context) error

	// Close releases all connections held by the watcher.
	Close() error
}

// DriftRecord is emitted when a watcher detects that live state no longer
// matches the last-known-good fingerprint.
type DriftRecord struct {
	WatcherName string
	Datasource  string
	Tenant      string
	Env         string
	OldHash     string
	NewHash     string
	DetectedAt  time.Time
}

// StateBackend is the persistence interface for watcher baselines.
// Implemented by *StateStore (disk) and *RedisStateStore (Redis).
type StateBackend interface {
	Load(key string) string
	Save(key, fingerprint string) error
}

// StateStore persists fingerprints between poll cycles so the watcher
// survives restarts without losing its baseline.
type StateStore struct {
	dir string
}

// NewStateStore creates a StateStore rooted at dir.
// The directory is created if it does not exist.
func NewStateStore(dir string) (*StateStore, error) {
	if err := os.MkdirAll(dir, 0o750); err != nil {
		return nil, fmt.Errorf("state store: mkdir %s: %w", dir, err)
	}
	return &StateStore{dir: dir}, nil
}

type stateFile struct {
	Fingerprint string    `json:"fingerprint"`
	UpdatedAt   time.Time `json:"updated_at"`
}

// Load returns the persisted fingerprint for key, or "" if not found.
func (s *StateStore) Load(key string) string {
	data, err := os.ReadFile(s.path(key))
	if err != nil {
		return ""
	}
	var f stateFile
	if err := json.Unmarshal(data, &f); err != nil {
		return ""
	}
	return f.Fingerprint
}

// Save persists a fingerprint for key.
func (s *StateStore) Save(key, fingerprint string) error {
	f := stateFile{Fingerprint: fingerprint, UpdatedAt: time.Now().UTC()}
	data, _ := json.MarshalIndent(f, "", "  ")
	return os.WriteFile(s.path(key), data, 0o640)
}

func (s *StateStore) path(key string) string {
	// Sanitise key to make it filename-safe.
	safe := ""
	for _, c := range key {
		if (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z') ||
			(c >= '0' && c <= '9') || c == '-' || c == '_' {
			safe += string(c)
		} else {
			safe += "_"
		}
	}
	return filepath.Join(s.dir, safe+".json")
}

// ─── Fingerprint helpers ─────────────────────────────────────────────────────

// FingerprintRecords produces a SHA-256 hex digest of the given records.
//
// Records are sorted by key so the hash is deterministic regardless of
// the order the datasource returns them.
func FingerprintRecords(records map[string]string) string {
	keys := make([]string, 0, len(records))
	for k := range records {
		keys = append(keys, k)
	}
	sort.Strings(keys)

	h := sha256.New()
	for _, k := range keys {
		h.Write([]byte(k))
		h.Write([]byte("="))
		h.Write([]byte(records[k]))
		h.Write([]byte("\n"))
	}
	return hex.EncodeToString(h.Sum(nil))
}

// ─── VarTrack metadata constants ─────────────────────────────────────────────
//
// These match the labels written by orchestrator-service/app/pipeline/sinks/labels.py
// for each storage backend.

const (
	// MongoDB _vt_* meta fields
	VTManagedByField  = "_vt_managed_by"
	VTTenantField     = "_vt_tenant"
	VTDatasourceField = "_vt_datasource"
	VTEnvField        = "_vt_env"
	VTCommitField     = "_vt_commit"
	VTFileField       = "_vt_file"
	VTBranchField     = "_vt_branch"
	VTRepoField       = "_vt_repo"

	// The fixed value stored in all _vt_managed_by / managed-by fields.
	ManagedByValue = "vartrack"

	// ZooKeeper metadata znode name
	ZKMetaZnode = "__vartrack__"

	// ZooKeeper watcher state znode
	ZKStateZnode = "__watcher_state__"

	// S3 tag key for managed-by
	S3ManagedByTag = "app.kubernetes.io%2Fmanaged-by"

	// S3 object name for watcher state
	S3StateObject = "__vartrack__/watcher_state.json"
)

// ─── Logging helpers ─────────────────────────────────────────────────────────

// logDrift logs a drift event at WARN level.
func LogDrift(dr DriftRecord) {
	oldHash, newHash := dr.OldHash, dr.NewHash
	if len(oldHash) > 8 {
		oldHash = oldHash[:8]
	}
	if len(newHash) > 8 {
		newHash = newHash[:8]
	}
	slog.Warn("watcher: drift detected",
		"watcher", dr.WatcherName,
		"datasource", dr.Datasource,
		"tenant", dr.Tenant,
		"env", dr.Env,
		"old_hash", oldHash,
		"new_hash", newHash,
		"detected_at", dr.DetectedAt.UTC().Format(time.RFC3339),
	)
}
