package bundle

import (
	"fmt"
	"sort"
	"strings"

	"google.golang.org/protobuf/reflect/protoreflect"

	pb_models "vartrack/libs/gen/vartrack/v1/models"
)

// checkSyncLimits refuses sync_limits that cannot work, with the limit and the key named:
//
//   - two limits with one name (the name is what the UI, the logs and the metrics show);
//   - a datasource in two limits: a sync would need a slot of each, and two syncs taking them
//     in opposite orders would wait on each other;
//   - a datasource the bundle does not declare, so a typo does not leave a store unlimited.
//
// A fragment (a docs snippet, a bundle in parts) cannot say which datasources exist,
// so for it the last check is left out.
func checkSyncLimits(bundle *pb_models.Bundle, fragment bool) error {
	limits := bundle.GetSyncLimits()
	if len(limits) == 0 {
		return nil
	}

	declared := datasourceNames(bundle)
	byName := map[string]int{}
	owner := map[string]int{}
	for i, limit := range limits {
		// The proto's own rules (a name, a datasource, max_at_once of at least 1) are not run on a value that is
		// proto3's default: 0 and "" read as "not set". They are said here, with the key named.
		if limit.GetName() == "" {
			return fmt.Errorf("sync_limits[%d].name is required: it is what the UI, the logs and the metrics call the limit", i)
		}
		if len(limit.GetDatasources()) == 0 {
			return fmt.Errorf("sync_limits[%d].datasources must name at least one datasource (limit %s)", i, limit.GetName())
		}
		if limit.GetMaxAtOnce() < 1 {
			return fmt.Errorf("sync_limits[%d].max_at_once must be at least 1 (limit %s): it is how many syncs may write at once",
				i, limit.GetName())
		}
		if j, seen := byName[limit.GetName()]; seen {
			return fmt.Errorf("sync_limits[%d] repeats the name %q (sync_limits[%d]): a limit is known by its name",
				i, limit.GetName(), j)
		}
		byName[limit.GetName()] = i

		for _, name := range limit.GetDatasources() {
			if j, seen := owner[name]; seen {
				return fmt.Errorf("sync_limits[%d] (%s) names the datasource %q, which sync_limits[%d] (%s) already limits: "+
					"a datasource is in at most one limit, so put the datasources that share a server in one",
					i, limit.GetName(), name, j, limits[j].GetName())
			}
			owner[name] = i
			if !fragment && !declared[name] {
				return fmt.Errorf("sync_limits[%d] (%s) names the datasource %q, which the bundle does not declare (declared: %s)",
					i, limit.GetName(), name, strings.Join(sortedKeys(declared), ", "))
			}
		}
	}
	return nil
}

// datasourceNames are the full names the bundle's datasources go by, as a rule names them: the type
// ("mongo"), or the type and the tag ("mongo-primary").
func datasourceNames(bundle *pb_models.Bundle) map[string]bool {
	names := map[string]bool{}
	for _, ds := range bundle.GetDatasources() {
		msg := ds.ProtoReflect()
		oneof := msg.Descriptor().Oneofs().ByName("config")
		if oneof == nil {
			continue
		}
		set := msg.WhichOneof(oneof)
		if set == nil {
			continue
		}
		name := string(set.Name())
		if tag := tagOf(msg.Get(set).Message()); tag != "" {
			name += "-" + tag
		}
		names[name] = true
	}
	return names
}

// tagOf is the tag of a datasource's config, "" when it has none.
func tagOf(config protoreflect.Message) string {
	field := config.Descriptor().Fields().ByName("tag")
	if field == nil || field.Kind() != protoreflect.StringKind || !config.Has(field) {
		return ""
	}
	return config.Get(field).String()
}

func sortedKeys(set map[string]bool) []string {
	keys := make([]string, 0, len(set))
	for k := range set {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}
