package bundle

import (
	"fmt"
	"log"
	"sort"

	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
	"google.golang.org/protobuf/types/descriptorpb"

	vartrackv1 "vartrack/libs/gen/vartrack/v1"
	pb "vartrack/libs/gen/vartrack/v1/models"
	datasources "vartrack/libs/gen/vartrack/v1/models/datasources"
)

// The names an option had before it was renamed are stated on the option, in
// the proto ((vartrack.v1.config).old_names), and read from there. The
// orchestrator reads the same annotation, so the services cannot disagree on
// which old names still work.
var (
	renamedRuleOptions = renamedOptions((&pb.Rule{}).ProtoReflect().Descriptor())
	renamedS3Options   = renamedOptions((&datasources.S3Config{}).ProtoReflect().Descriptor())
)

// renamedOptions maps each old name of msg's options to the option's current
// name.
func renamedOptions(msg protoreflect.MessageDescriptor) map[string]string {
	out := map[string]string{}
	fields := msg.Fields()
	for i := 0; i < fields.Len(); i++ {
		field := fields.Get(i)
		options, _ := field.Options().(*descriptorpb.FieldOptions)
		config, _ := proto.GetExtension(options, vartrackv1.E_Config).(*vartrackv1.ConfigOption)
		for _, old := range config.GetOldNames() {
			out[old] = string(field.Name())
		}
	}
	return out
}

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

// Options the proto no longer has, and why they went. A config.cue that still
// sets one keeps loading under the strict parse: it is dropped, with the
// reason in the log.
var (
	removedRuleOptions = map[string]string{
		"retry_backoff": "nothing read it",
		"secrets": "declare the secret manager under secret_managers (path_prefix is set there) " +
			"and name it in the rule's secret_manager",
	}
	removedBundleOptions = map[string]string{
		"watcher_state_datasource": "the watcher keeps no state between polls",
	}
)

// removedPruneLast: prune.last asked to delay deleting until all files were
// written; prune now always runs once per environment, after them. A prune
// block that only had it is left empty, which is a disabled block.
const removedPruneLast = "prune now runs once per environment, after all its files are written"

// dropRemovedOptions removes the options the proto no longer has (see above).
// Returns what it dropped.
func dropRemovedOptions(bundle map[string]interface{}) []string {
	dropped := map[string]string{} // path → why
	for key, why := range removedBundleOptions {
		if _, ok := bundle[key]; ok {
			delete(bundle, key)
			dropped[key] = why
		}
	}
	rules, _ := bundle["rules"].([]interface{})
	for i, r := range rules {
		rule, _ := r.(map[string]interface{})
		for key, why := range removedRuleOptions {
			if _, ok := rule[key]; ok {
				delete(rule, key)
				dropped[fmt.Sprintf("rules[%d].%s", i, key)] = why
			}
		}
		prune, _ := rule["prune"].(map[string]interface{})
		if _, ok := prune["last"]; ok {
			delete(prune, "last")
			dropped[fmt.Sprintf("rules[%d].prune.last", i)] = removedPruneLast
		}
	}

	paths := make([]string, 0, len(dropped))
	for path := range dropped {
		paths = append(paths, path)
	}
	sort.Strings(paths)
	for _, path := range paths {
		log.Printf("bundle: %s was removed and has no effect: %s", path, dropped[path])
	}
	return paths
}
