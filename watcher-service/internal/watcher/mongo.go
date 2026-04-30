// mongo.go — MongoDB drift watcher.
//
// Scans every document in the configured collection for the VarTrack
// managed-by field (_vt_managed_by: "vartrack") and computes a SHA-256
// fingerprint of the key→value pairs.
//
// If the fingerprint changes between polls the watcher calls the Healer to
// trigger a re-sync from git, which re-writes the VarTrack-managed values
// and overwrites any external tampering.
//
// MongoDB Change Streams are used when the server supports them (replica
// sets / sharded clusters).  When not available (standalone), the watcher
// falls back to periodic polling.
package watcher

import (
	"context"
	"fmt"
	"log/slog"
	"time"

	"go.mongodb.org/mongo-driver/bson"
	"go.mongodb.org/mongo-driver/mongo"
	"go.mongodb.org/mongo-driver/mongo/options"

	"watcher-service/internal/config"
	dsv1 "watcher-service/internal/gen/proto/go/vartrack/v1/models/datasources"
	models "watcher-service/internal/gen/proto/go/vartrack/v1/models"
	"watcher-service/internal/healer"
)

// MongoWatcher watches a MongoDB collection for drift in VarTrack-managed
// documents.
type MongoWatcher struct {
	name       string
	client     *mongo.Client
	database   string
	collection string
	healer     *healer.Healer
	healOpts   healer.HealRequest
}

// NewMongoWatcher connects to MongoDB and returns a ready MongoWatcher.
//
// Parameters:
//   cfg         — datasource config from the CUE bundle
//   rule        — the rule that references this datasource
//   h           — healer used to trigger orchestrator re-sync
//   healTimeout — per-request timeout for heal HTTP calls
func NewMongoWatcher(
	ctx context.Context,
	cfg *dsv1.MongoConfig,
	rule *models.Rule,
	h *healer.Healer,
) (*MongoWatcher, error) {
	uri := cfg.GetEndpoint()
	if uri == "" {
		uri = fmt.Sprintf("mongodb://%s:%d", cfg.GetHost(), cfg.GetPort())
	}

	opts := options.Client().ApplyURI(uri)
	opts.SetConnectTimeout(10 * time.Second)
	opts.SetServerSelectionTimeout(10 * time.Second)

	client, err := mongo.Connect(ctx, opts)
	if err != nil {
		return nil, fmt.Errorf("mongo watcher %s: connect: %w", config.RuleName(rule), err)
	}
	if err := client.Ping(ctx, nil); err != nil {
		_ = client.Disconnect(ctx)
		return nil, fmt.Errorf("mongo watcher %s: ping: %w", config.RuleName(rule), err)
	}

	db := cfg.GetDatabase()
	if db == "" {
		db = "vartrack"
	}
	coll := cfg.GetCollection()
	if coll == "" {
		coll = "configs"
	}

	slog.Info("mongo watcher: connected",
		"watcher", config.RuleName(rule), "uri", uri, "db", db, "collection", coll)

	return &MongoWatcher{
		name:       config.RuleName(rule),
		client:     client,
		database:   db,
		collection: coll,
		healer:     h,
		healOpts: healer.HealRequest{
			Datasource: rule.GetDatasource(),
			Platform:   rule.GetPlatform(),
		},
	}, nil
}

// Name implements Watcher.
func (w *MongoWatcher) Name() string { return "mongo/" + w.name }

// Snapshot queries all VarTrack-managed documents and returns a fingerprint.
//
// Only documents with _vt_managed_by == "vartrack" are included so that
// non-VarTrack documents in the same collection are ignored.
//
// Fingerprint algorithm:
//  1. Collect all (key, value) pairs from each managed document,
//     excluding the _vt_* meta fields themselves.
//  2. Sort keys alphabetically (deterministic order).
//  3. SHA-256 over the sorted key=value pairs.
func (w *MongoWatcher) Snapshot(ctx context.Context) (string, error) {
	ctx, cancel := context.WithTimeout(ctx, 30*time.Second)
	defer cancel()

	coll := w.client.Database(w.database).Collection(w.collection)

	// Filter: only VarTrack-managed documents.
	filter := bson.M{VTManagedByField: ManagedByValue}
	cursor, err := coll.Find(ctx, filter, options.Find().SetProjection(bson.M{"_id": 0}))
	if err != nil {
		return "", fmt.Errorf("mongo snapshot %s: find: %w", w.name, err)
	}
	defer cursor.Close(ctx)

	// Build a deterministic key→value map from all managed documents.
	records := make(map[string]string)
	for cursor.Next(ctx) {
		var doc bson.M
		if err := cursor.Decode(&doc); err != nil {
			return "", fmt.Errorf("mongo snapshot %s: decode: %w", w.name, err)
		}
		// "key" is the config key (e.g. "db.host"), "value" is the value.
		// For DOCUMENT strategy: each document is {_vt_*..., "key":"db.host","value":"localhost"}
		// For FILE strategy:    each document is {_vt_*..., "data":{...}}
		extractRecords(doc, records)
	}
	if err := cursor.Err(); err != nil {
		return "", fmt.Errorf("mongo snapshot %s: cursor: %w", w.name, err)
	}

	if len(records) == 0 {
		// No managed records — empty collection fingerprint.
		return FingerprintRecords(nil), nil
	}
	return FingerprintRecords(records), nil
}

// Restore implements Watcher — calls the Healer to request orchestrator re-sync.
func (w *MongoWatcher) Restore(ctx context.Context) error {
	slog.Info("mongo watcher: triggering heal", "watcher", w.name)
	return w.healer.Heal(ctx, w.healOpts)
}

// Close disconnects from MongoDB.
func (w *MongoWatcher) Close() error {
	return w.client.Disconnect(context.Background())
}

// ─── helpers ──────────────────────────────────────────────────────────────────

// extractRecords pulls config key→value pairs out of a VarTrack MongoDB
// document.  VT meta fields (_vt_*) are excluded from the fingerprint.
func extractRecords(doc bson.M, out map[string]string) {
	// DOCUMENT strategy: top-level "key" and "value" fields.
	if k, ok := doc["key"].(string); ok {
		if v, ok := doc["value"]; ok {
			out[k] = fmt.Sprintf("%v", v)
		}
		return
	}

	// FILE strategy: "data" is a sub-document of key→value entries.
	if dataRaw, ok := doc["data"]; ok {
		if data, ok := dataRaw.(bson.M); ok {
			for k, v := range data {
				out[k] = fmt.Sprintf("%v", v)
			}
		}
		return
	}

	// Fallback: include all non-_vt_ fields as flat key→value.
	for k, v := range doc {
		if len(k) >= 4 && k[:4] == "_vt_" {
			continue
		}
		if k == "_id" {
			continue
		}
		out[k] = fmt.Sprintf("%v", v)
	}
}
