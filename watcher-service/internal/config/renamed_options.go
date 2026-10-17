package config

import (
	"fmt"
	"log"
	"sort"
)

// renamedRuleOptions and renamedS3Options are bundle options that were
// renamed. The old names are still read, so existing bundles keep loading.
var (
	renamedRuleOptions = map[string]string{
		"env_as_branch": "branch_as_env",
		"env_as_pr":     "pr_as_env",
		"env_as_tags":   "tag_as_env",
	}
	renamedS3Options = map[string]string{
		"env_as_prefix": "prefix_as_env",
	}
)

// upgradeRenamedOptions moves rule and S3 datasource options from their old
// names to the current ones, before the bundle is parsed against the proto.
// When both names are set, the current one wins. Returns what it moved.
func upgradeRenamedOptions(bundle map[string]interface{}) []string {
	var moved []string
	rename := func(m map[string]interface{}, names map[string]string, where string) {
		for old, current := range names {
			value, ok := m[old]
			if !ok {
				continue
			}
			delete(m, old)
			if _, set := m[current]; !set {
				m[current] = value
			}
			moved = append(moved, fmt.Sprintf("%s.%s is now %s", where, old, current))
		}
	}

	rules, _ := bundle["rules"].([]interface{})
	for i, r := range rules {
		if rule, ok := r.(map[string]interface{}); ok {
			rename(rule, renamedRuleOptions, fmt.Sprintf("rules[%d]", i))
		}
	}
	datasources, _ := bundle["datasources"].([]interface{})
	for i, d := range datasources {
		wrapper, _ := d.(map[string]interface{})
		if s3, ok := wrapper["s3"].(map[string]interface{}); ok {
			rename(s3, renamedS3Options, fmt.Sprintf("datasources[%d].s3", i))
		}
	}

	sort.Strings(moved)
	for _, m := range moved {
		log.Printf("bundle: %s; the old name still works", m)
	}
	return moved
}
