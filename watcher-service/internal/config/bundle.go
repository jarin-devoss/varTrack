// bundle.go — CUE bundle loader for the watcher-service.
//
// Loads the VarTrack CUE config bundle and unmarshals it directly into the
// proto-generated Bundle message using protojson.  The watcher re-uses the
// same types as the rest of the system instead of duplicating struct
// definitions.
//
// Run:
//
//	buf generate --template buf.gen.watcher.yaml
//
// from the repo root to regenerate the Go types under
// internal/gen/proto/go.
package config

import (
	"encoding/json"
	"fmt"
	"log"
	"strings"

	"cuelang.org/go/cue"
	"cuelang.org/go/cue/cuecontext"
	"cuelang.org/go/cue/load"
	"google.golang.org/protobuf/encoding/protojson"

	dsv1 "watcher-service/internal/gen/proto/go/vartrack/v1/models/datasources"
	models "watcher-service/internal/gen/proto/go/vartrack/v1/models"
)

// LoadBundle reads and parses the CUE bundle at cuePath into a proto Bundle.
func LoadBundle(cuePath string) (*models.Bundle, error) {
	jsonBytes, err := cueFileToJSON(cuePath)
	if err != nil {
		return nil, fmt.Errorf("load bundle: %w", err)
	}

	// Normalise plain-string SecretRef values to {"value":"..."} so that
	// protojson can unmarshal them into the SecretRef proto message.
	// Users can write  token: "ghp_xxx"  in CUE instead of the verbose
	// token: {value: "ghp_xxx"}  form; this step bridges the two.
	jsonBytes = normalizeSecretRefs(jsonBytes)

	b := &models.Bundle{}
	opts := protojson.UnmarshalOptions{DiscardUnknown: true}
	if err := opts.Unmarshal(jsonBytes, b); err != nil {
		return nil, fmt.Errorf("unmarshal bundle: %w", err)
	}
	return b, nil
}

// secretRefFieldNames is the set of JSON field names whose values may be either
// a plain string (user shorthand) or a SecretRef object (proto wire format).
var secretRefFieldNames = map[string]struct{}{
	"token": {}, "secret": {}, "password": {}, "username": {},
	"private_key": {}, "access_key_id": {}, "secret_access_key": {},
	"role_id": {}, "secret_id": {},
}

// normalizeSecretRefs walks raw JSON and converts any plain string value for a
// known SecretRef field name into {"value":"..."}, making it compatible with
// protojson unmarshaling into the SecretRef proto message.
func normalizeSecretRefs(data []byte) []byte {
	var raw interface{}
	if err := json.Unmarshal(data, &raw); err != nil {
		return data
	}
	out, err := json.Marshal(walkSecretRefs(raw))
	if err != nil {
		return data
	}
	return out
}

func walkSecretRefs(v interface{}) interface{} {
	switch val := v.(type) {
	case map[string]interface{}:
		out := make(map[string]interface{}, len(val))
		for k, child := range val {
			if _, isRef := secretRefFieldNames[k]; isRef {
				if s, ok := child.(string); ok {
					out[k] = map[string]interface{}{"value": s}
					continue
				}
			}
			out[k] = walkSecretRefs(child)
		}
		return out
	case []interface{}:
		out := make([]interface{}, len(val))
		for i, item := range val {
			out[i] = walkSecretRefs(item)
		}
		return out
	default:
		return v
	}
}

// SelfHealRules returns only rules with self_heal=true.
func SelfHealRules(b *models.Bundle) []*models.Rule {
	var out []*models.Rule
	for _, r := range b.GetRules() {
		if r.GetSelfHeal() {
			out = append(out, r)
		}
	}
	return out
}

// RuleName returns a human-readable rule identifier: "{platform}/{datasource}".
func RuleName(r *models.Rule) string {
	return r.GetPlatform() + "/" + r.GetDatasource()
}

// FindDatasource returns the DataSource whose resolved name matches name.
func FindDatasource(b *models.Bundle, name string) (*models.DataSource, bool) {
	for _, ds := range b.GetDatasources() {
		if datasourceName(ds) == name {
			return ds, true
		}
	}
	return nil, false
}

// datasourceName resolves "{type}-{tag}" or "{type}" for a datasource.
func datasourceName(ds *models.DataSource) string {
	switch {
	case ds.GetMongo() != nil:
		if tag := ds.GetMongo().GetTag(); tag != "" {
			return "mongo-" + tag
		}
		return "mongo"
	case ds.GetRedis() != nil:
		if tag := ds.GetRedis().GetTag(); tag != "" {
			return "redis-" + tag
		}
		return "redis"
	case ds.GetZookeeper() != nil:
		if tag := ds.GetZookeeper().GetTag(); tag != "" {
			return "zookeeper-" + tag
		}
		return "zookeeper"
	case ds.GetS3() != nil:
		if tag := ds.GetS3().GetTag(); tag != "" {
			return "s3-" + tag
		}
		return "s3"
	case ds.GetConfigmap() != nil:
		if tag := ds.GetConfigmap().GetTag(); tag != "" {
			return "configmap-" + tag
		}
		return "configmap"
	case ds.GetHelm() != nil:
		if tag := ds.GetHelm().GetTag(); tag != "" {
			return "helm-" + tag
		}
		return "helm"
	case ds.GetLinuxServer() != nil:
		if tag := ds.GetLinuxServer().GetTag(); tag != "" {
			return "linux_server-" + tag
		}
		return "linux_server"
	case ds.GetVercel() != nil:
		if tag := ds.GetVercel().GetTag(); tag != "" {
			return "vercel-" + tag
		}
		return "vercel"
	}
	return "unknown"
}

// SinkType infers the canonical sink kind from a datasource name string.
func SinkType(datasource string) string {
	for _, prefix := range []string{
		"mongo", "redis", "zookeeper", "s3",
		"configmap", "helm", "linux_server", "vercel",
	} {
		if datasource == prefix ||
			len(datasource) > len(prefix)+1 && datasource[:len(prefix)+1] == prefix+"-" {
			return prefix
		}
	}
	return datasource
}

// RedisURLFromBundle loads the CUE bundle at configPath, resolves the
// datasource tag stored in bundle.watcher_state_datasource, and returns a
// redis:// URL for that datasource.
//
// Returns "" when the field is absent or no matching datasource is found.
// Logs a warning on error so callers can fall back to the in-memory state store.
func RedisURLFromBundle(configPath string) string {
	if configPath == "" {
		return ""
	}
	bundle, err := LoadBundle(configPath)
	if err != nil {
		log.Printf("bundle: cannot load %q: %v", configPath, err)
		return ""
	}
	// GetWatcherStateDatasource returns a datasource name like "redis-broker".
	// Derive the tag: "redis-broker" → "broker", "redis" → "".
	dsName := bundle.GetWatcherStateDatasource()
	if dsName == "" {
		return ""
	}
	tag := strings.TrimPrefix(dsName, "redis-")
	if tag == dsName {
		tag = "" // bare "redis" datasource, no tag suffix
	}
	for _, ds := range bundle.GetDatasources() {
		r := ds.GetRedis()
		if r == nil || r.GetTag() != tag {
			continue
		}
		return redisURL(r)
	}
	log.Printf("bundle: no redis datasource %q (watcher_state_datasource) in %q", dsName, configPath)
	return ""
}

// redisURL builds a redis:// connection URL from a proto RedisConfig.
func redisURL(r *dsv1.RedisConfig) string {
	host := "localhost:6379"
	if hosts := r.GetHosts(); len(hosts) > 0 && hosts[0] != "" {
		host = hosts[0]
	}
	auth := ""
	if pw := r.GetPassword().GetValue(); pw != "" {
		auth = fmt.Sprintf(":%s@", pw)
	}
	return fmt.Sprintf("redis://%s%s/%d", auth, host, r.GetDatabase())
}

// ─── Leader election ─────────────────────────────────────────────────────────

// ElectionConfig carries the resolved connection parameters for leader election.
// Exactly one of RedisURL or ZKHosts will be non-empty.
type ElectionConfig struct {
	// RedisURL is set when the election datasource is Redis.
	RedisURL string

	// ZKHosts and ZKPath are set when the election datasource is ZooKeeper.
	ZKHosts []string
	ZKPath  string
}

// leaderElectionJSON is used to extract watcher_leader_election_datasource
// from the raw bundle JSON before the proto getter is available post-regen.
type leaderElectionJSON struct {
	WatcherLeaderElectionDatasource string `json:"watcher_leader_election_datasource"`
}

// LeaderElectionFromBundle reads watcher_leader_election_datasource from the
// CUE bundle and resolves it to an ElectionConfig.
//
// Supported datasource types and their election mechanism:
//   - Redis     — SET NX PX distributed lock (DB 4)
//   - ZooKeeper — ephemeral sequential znode recipe
//
// Returns nil when the field is absent or the datasource type is not supported
// for leader election (MongoDB, S3, Helm, ConfigMap, Vercel, linux_server
// do not expose the atomic primitives required for safe leader election).
func LeaderElectionFromBundle(configPath string) *ElectionConfig {
	if configPath == "" {
		return nil
	}

	raw, err := cueFileToJSON(configPath)
	if err != nil {
		log.Printf("bundle: cannot load %q for leader election: %v", configPath, err)
		return nil
	}

	var meta leaderElectionJSON
	if err := json.Unmarshal(raw, &meta); err != nil || meta.WatcherLeaderElectionDatasource == "" {
		return nil
	}

	dsName := meta.WatcherLeaderElectionDatasource
	sinkType := SinkType(dsName)

	bundle, err := LoadBundle(configPath)
	if err != nil {
		log.Printf("bundle: cannot load %q: %v", configPath, err)
		return nil
	}

	switch sinkType {
	case "redis":
		tag := strings.TrimPrefix(dsName, "redis-")
		if tag == dsName {
			tag = ""
		}
		for _, ds := range bundle.GetDatasources() {
			r := ds.GetRedis()
			if r == nil || r.GetTag() != tag {
				continue
			}
			return &ElectionConfig{RedisURL: redisURL(r)}
		}
		log.Printf("bundle: leader election: redis datasource %q not found", dsName)

	case "zookeeper":
		tag := strings.TrimPrefix(dsName, "zookeeper-")
		if tag == dsName {
			tag = ""
		}
		for _, ds := range bundle.GetDatasources() {
			z := ds.GetZookeeper()
			if z == nil || z.GetTag() != tag {
				continue
			}
			return &ElectionConfig{
				ZKHosts: z.GetHosts(),
				ZKPath:  "/vartrack/watcher/election",
			}
		}
		log.Printf("bundle: leader election: zookeeper datasource %q not found", dsName)

	default:
		log.Printf("bundle: leader election: datasource type %q is not supported "+
			"(only redis and zookeeper support distributed leader election)", sinkType)
	}

	return nil
}

// ─── CUE helper ──────────────────────────────────────────────────────────────

func cueFileToJSON(cuePath string) ([]byte, error) {
	cfg := &load.Config{}
	instances := load.Instances([]string{cuePath}, cfg)
	if len(instances) == 0 {
		return nil, fmt.Errorf("no CUE instances found at %s", cuePath)
	}
	if instances[0].Err != nil {
		return nil, fmt.Errorf("load CUE: %w", instances[0].Err)
	}

	ctx := cuecontext.New()
	value := ctx.BuildInstance(instances[0])
	if value.Err() != nil {
		return nil, fmt.Errorf("build CUE: %w", value.Err())
	}

	bundleVal := value.LookupPath(cue.ParsePath("bundle"))
	if bundleVal.Err() != nil {
		return nil, fmt.Errorf("bundle key not found: %w", bundleVal.Err())
	}
	if err := bundleVal.Validate(cue.Concrete(true)); err != nil {
		return nil, fmt.Errorf("bundle validation: %w", err)
	}

	out, err := bundleVal.MarshalJSON()
	if err != nil {
		return nil, fmt.Errorf("marshal bundle to JSON: %w", err)
	}
	return out, nil
}
