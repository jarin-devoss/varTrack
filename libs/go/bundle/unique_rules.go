package bundle

import (
	"fmt"

	pb_models "vartrack/libs/gen/vartrack/v1/models"
)

// checkUniqueRules refuses a bundle in which two rules share a platform and
// a datasource. A rule is identified by that pair: webhooks, the CLI and the
// web UI all look a rule up by it and take the first match, so a second rule
// would never run, and nothing would say so.
func checkUniqueRules(bundle *pb_models.Bundle) error {
	first := map[[2]string]int{}
	for i, r := range bundle.GetRules() {
		key := [2]string{r.GetPlatform(), r.GetDatasource()}
		if j, seen := first[key]; seen {
			return fmt.Errorf("rules[%d] repeats %s/%s (rules[%d]): a rule is identified by its platform and "+
				"datasource; to write a second destination in the same database, declare a tagged datasource "+
				"(for example %s-db) and give it its own rule",
				i, key[0], key[1], j, datasourceType(key[1]))
		}
		first[key] = i
	}
	return nil
}

// datasourceType is the type of a datasource name: "mongo-primary" is a mongo.
func datasourceType(name string) string {
	for i, c := range name {
		if c == '-' {
			return name[:i]
		}
	}
	return name
}
