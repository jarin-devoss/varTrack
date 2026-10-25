// election.go — the one thing the watcher reads config.cue for: leader
// election.
//
// The watcher does not read rules from the bundle. The orchestrator says which
// rules to watch (Watcher.ListWatchTargets), because it is the one service
// that layers config.cue, bundle.json and vartrack.json into a rule. What is
// left of config.cue for the watcher is `watcher_leader_election_datasource`:
// which Redis or ZooKeeper several watcher replicas elect a leader on. That is
// part of the bundle's infrastructure wiring, next to `celery_broker_datasource`
// and `gateway_nonce_datasource`, so it is named there and not twice.
package config

import (
	"errors"
	"fmt"
	"log"
	"net/url"
	"strconv"
	"strings"

	"vartrack/libs/bundle"
	dsv1 "vartrack/libs/gen/vartrack/v1/models/datasources"
	utils "vartrack/libs/gen/vartrack/v1/utils"
	"vartrack/libs/secrets"
)

// ElectionConfig carries the resolved connection parameters for leader election.
// Exactly one of RedisURL or ZKHosts will be non-empty.
type ElectionConfig struct {
	// RedisURL is set when the election datasource is Redis.
	RedisURL string

	// ZKHosts and ZKPath are set when the election datasource is ZooKeeper.
	ZKHosts []string
	ZKPath  string
}

// LeaderElection reads watcher_leader_election_datasource from the CUE bundle
// at configPath and resolves it to an ElectionConfig.
//
// Supported datasource types and their election mechanism:
//   - Redis     — SET NX PX distributed lock (DB 4)
//   - ZooKeeper — ephemeral sequential znode recipe
//
// It returns nil, with no error, when no election datasource is named, when it
// is not found, or when its type is not supported (MongoDB, S3, Helm,
// ConfigMap, Vercel and linux_server do not expose the atomic primitives safe
// leader election needs): the watcher then runs standalone. It returns an
// error when the bundle cannot be read, or the election datasource's password
// cannot be had — starting without the election it was told to use would let
// every replica heal at once.
func LeaderElection(configPath string) (*ElectionConfig, error) {
	if configPath == "" {
		return nil, nil
	}
	// Lenient: the watcher reads only this part of the bundle, and must keep
	// working when another service's settings change.
	b, err := bundle.Load(configPath, bundle.Options{})
	if err != nil {
		return nil, fmt.Errorf("load bundle %s: %w", configPath, err)
	}

	dsName := b.GetWatcherLeaderElectionDatasource()
	if dsName == "" {
		return nil, nil
	}

	switch kind := sinkType(dsName); kind {
	case "redis":
		tag := strings.TrimPrefix(dsName, "redis-")
		if tag == dsName {
			tag = ""
		}
		for _, ds := range b.GetDatasources() {
			r := ds.GetRedis()
			if r == nil || r.GetTag() != tag {
				continue
			}
			redisURL, err := redisURL(r)
			if err != nil {
				return nil, fmt.Errorf("leader election: redis datasource %q: %w", dsName, err)
			}
			return &ElectionConfig{RedisURL: redisURL}, nil
		}
		log.Printf("bundle: leader election: redis datasource %q not found", dsName)

	case "zookeeper":
		tag := strings.TrimPrefix(dsName, "zookeeper-")
		if tag == dsName {
			tag = ""
		}
		for _, ds := range b.GetDatasources() {
			z := ds.GetZookeeper()
			if z == nil || z.GetTag() != tag {
				continue
			}
			return &ElectionConfig{
				ZKHosts: z.GetHosts(),
				ZKPath:  "/vartrack/watcher/election",
			}, nil
		}
		log.Printf("bundle: leader election: zookeeper datasource %q not found", dsName)

	default:
		log.Printf("bundle: leader election: datasource type %q is not supported "+
			"(only redis and zookeeper support distributed leader election)", kind)
	}

	return nil, nil
}

// sinkType infers the canonical sink kind from a datasource name string.
func sinkType(datasource string) string {
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

// redisURL builds a redis:// connection URL from a proto RedisConfig, for
// leader election. The password may be written inline or as a {file}; a {ref}
// needs a secret manager, which the watcher does not have.
func redisURL(r *dsv1.RedisConfig) (string, error) {
	host := "localhost:6379"
	if hosts := r.GetHosts(); len(hosts) > 0 && hosts[0] != "" {
		host = hosts[0]
	}
	password, err := credential(r.GetPassword())
	if err != nil {
		return "", fmt.Errorf("password: %w", err)
	}
	u := url.URL{Scheme: "redis", Host: host, Path: "/" + strconv.Itoa(int(r.GetDatabase()))}
	if password != "" {
		u.User = url.UserPassword("", password) // escaped: a password may hold @ or /
	}
	return u.String(), nil
}

// credential is a SecretRef's value: inline, or read from the file it names.
func credential(ref *utils.SecretRef) (string, error) {
	switch source := ref.GetSource().(type) {
	case *utils.SecretRef_Value:
		return source.Value, nil
	case *utils.SecretRef_File:
		return secrets.ReadSecretFile(source.File, ref.GetKey())
	case *utils.SecretRef_Ref:
		return "", errors.New("a {ref} password needs a secret manager, which the watcher does not have; use {value} or {file}")
	}
	return "", nil
}
