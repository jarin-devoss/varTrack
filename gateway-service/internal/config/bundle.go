package config

import (
	"fmt"
	"log"
	"os"
	"strings"

	"cuelang.org/go/cue"
	"cuelang.org/go/cue/cuecontext"
)

// RedisURLFromBundle reads the CUE bundle at configPath, looks up the
// datasource tag stored in bundle.gateway_nonce_datasource, and returns
// a redis:// URL built from that datasource's connection details.
//
// Returns "" (no error) when:
//   - configPath is empty
//   - gateway_nonce_datasource is absent or empty
//   - no datasource matches the tag
//
// Logs a warning (but does not return an error) on parse failures so that
// a mis-formatted bundle does not prevent service startup — Redis is
// optional for the gateway (nonce store falls back to in-memory).
func RedisURLFromBundle(configPath string) string {
	if configPath == "" {
		return ""
	}

	data, err := os.ReadFile(configPath)
	if err != nil {
		log.Printf("bundle: cannot read CUE file %q: %v", configPath, err)
		return ""
	}

	ctx := cuecontext.New()
	v := ctx.CompileBytes(data)
	if v.Err() != nil {
		log.Printf("bundle: cannot compile CUE file %q: %v", configPath, v.Err())
		return ""
	}

	// Resolve the datasource name from the dedicated bundle field.
	// The value follows the {type}-{tag} convention, e.g. "redis-broker".
	dsNameVal := v.LookupPath(cue.ParsePath("bundle.gateway_nonce_datasource"))
	if !dsNameVal.Exists() {
		return ""
	}
	dsName, err := dsNameVal.String()
	if err != nil || dsName == "" {
		return ""
	}

	// Derive the tag: "redis-broker" → "broker", "redis" → "".
	tag := strings.TrimPrefix(dsName, "redis-")
	if tag == dsName {
		tag = "" // no "redis-" prefix — bare "redis" datasource
	}

	// Scan bundle.datasources for a redis entry with the matching tag.
	datasources := v.LookupPath(cue.ParsePath("bundle.datasources"))
	if !datasources.Exists() {
		return ""
	}
	iter, err := datasources.List()
	if err != nil {
		log.Printf("bundle: cannot iterate datasources in %q: %v", configPath, err)
		return ""
	}

	for iter.Next() {
		entry := iter.Value()
		redisCfg := entry.LookupPath(cue.ParsePath("redis"))
		if !redisCfg.Exists() {
			continue
		}
		entryTag, _ := redisCfg.LookupPath(cue.ParsePath("tag")).String()
		if entryTag != tag {
			continue
		}
		url, err := buildRedisURL(redisCfg)
		if err != nil {
			log.Printf("bundle: building Redis URL for datasource %q: %v", dsName, err)
			return ""
		}
		return url
	}

	log.Printf("bundle: no redis datasource %q (gateway_nonce_datasource) in %q", dsName, configPath)
	return ""
}

// buildRedisURL constructs a redis:// URL from a CUE redis datasource value.
func buildRedisURL(v cue.Value) (string, error) {
	host := "localhost:6379"
	if hostsVal := v.LookupPath(cue.ParsePath("hosts")); hostsVal.Exists() {
		iter, err := hostsVal.List()
		if err == nil && iter.Next() {
			if h, err := iter.Value().String(); err == nil && h != "" {
				host = h
			}
		}
	}

	db := int64(0)
	if dbVal := v.LookupPath(cue.ParsePath("database")); dbVal.Exists() {
		if d, err := dbVal.Int64(); err == nil {
			db = d
		}
	}

	auth := ""
	if pwVal := v.LookupPath(cue.ParsePath("password")); pwVal.Exists() {
		if pw, err := pwVal.String(); err == nil && pw != "" {
			auth = fmt.Sprintf(":%s@", pw)
		}
	}

	return fmt.Sprintf("redis://%s%s/%d", auth, host, db), nil
}
