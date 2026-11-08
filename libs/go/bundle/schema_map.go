package bundle

import (
	"fmt"
	"strings"

	pb_models "vartrack/libs/gen/vartrack/v1/models"
)

// checkSchemaMaps refuses a rule whose schema_map has an entry that cannot work: a match that is not a glob,
// or a schema that is not "none" or a relative path to a .cue file inside the schema repository. Every bad
// entry of every rule is named in one error. The orchestrator makes the same checks (app/pipeline/schema_paths.py)
// on a tenant's bundle.json, so one entry is judged the same way wherever it is written.
func checkSchemaMaps(bundle *pb_models.Bundle) error {
	var problems []string
	for ri, rule := range bundle.GetRules() {
		for i, entry := range rule.GetSchemaMap() {
			where := fmt.Sprintf("rules[%d].schema_map[%d]", ri, i)
			if p := globProblem(entry.GetMatch()); p != "" {
				problems = append(problems, fmt.Sprintf("%s.match %q: %s", where, entry.GetMatch(), p))
			}
			if p := schemaProblem(entry.GetSchema()); p != "" {
				problems = append(problems, fmt.Sprintf("%s.schema %q: %s", where, entry.GetSchema(), p))
			}
		}
	}
	if len(problems) > 0 {
		return fmt.Errorf("schema_map: %s", strings.Join(problems, "; "))
	}
	return nil
}

// globProblem says why a schema_map match is not a usable glob, or "" when it is. A glob has `*`, `**`, `?`
// and `[...]` classes, over forward-slash paths in the repository.
func globProblem(pattern string) string {
	switch {
	case pattern == "":
		return "is empty"
	case strings.Contains(pattern, `\`):
		return `has a backslash: paths use "/"`
	case strings.HasPrefix(pattern, "/"):
		return "starts with /: a path in the repository is relative to its root"
	case strings.Contains(pattern, "***"):
		return `has "***"`
	}
	for i := 0; i < len(pattern); i++ {
		if pattern[i] != '[' {
			continue
		}
		end := strings.IndexByte(pattern[i+1:], ']')
		if end < 0 {
			return `has a "[" with no closing "]"`
		}
		if end == 0 {
			return `has an empty "[]" class`
		}
		i += end + 1
	}
	return ""
}

// schemaProblem says why a schema_map schema is not usable, or "" when it is: "none", or a relative path of
// "/"-separated names ending in .cue, with no "..".
func schemaProblem(schema string) string {
	if schema == "none" {
		return ""
	}
	switch {
	case schema == "":
		return `is empty (a path to a .cue file, or "none")`
	case strings.Contains(schema, `\`):
		return `has a backslash: paths use "/"`
	case strings.HasPrefix(schema, "/") || (len(schema) > 1 && schema[1] == ':'):
		return "is an absolute path: it is relative to the schema repository"
	case !strings.HasSuffix(schema, ".cue"):
		return `does not end in ".cue"`
	case strings.ContainsRune(schema, 0):
		return "has a NUL byte"
	}
	for _, part := range strings.Split(schema, "/") {
		switch part {
		case "..":
			return `has a ".." segment: it cannot leave the schema repository`
		case "":
			return `has an empty segment ("//")`
		}
	}
	return ""
}
