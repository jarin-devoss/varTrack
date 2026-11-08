package bundle

import (
	"fmt"
	"regexp"
	"strings"

	pb_models "vartrack/libs/gen/vartrack/v1/models"
)

// tenantName is the shape of a tenant name, the same one the CLI, the gateway and the
// orchestrator check on every request.
var tenantName = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$`)

// checkTenants refuses a bundle whose declared tenants contradict each other or the rules:
// a name that is not a tenant name, two tenants with one name, a repository or a schema
// repository that belongs to two tenants, a destination of one datasource that two tenants
// may both write, and a rule that names a tenant nobody declared. A bundle that declares no
// tenants has nothing to check: the implicit "default" tenant covers everything.
func checkTenants(bundle *pb_models.Bundle) error {
	declared := bundle.GetTenants()
	if len(declared) == 0 {
		return nil
	}

	index := map[string]int{}
	for i, t := range declared {
		if !tenantName.MatchString(t.GetName()) {
			return fmt.Errorf("tenants[%d] name %q is not a tenant name: letters, digits, '-' and '_' only, "+
				"starting with a letter or digit, up to 63 characters", i, t.GetName())
		}
		if j, seen := index[t.GetName()]; seen {
			return fmt.Errorf("tenants[%d] repeats the name %q (tenants[%d])", i, t.GetName(), j)
		}
		index[t.GetName()] = i
	}

	if err := checkTenantRepositories(declared); err != nil {
		return err
	}
	if err := checkTenantSchemaRepos(declared); err != nil {
		return err
	}
	if err := checkTenantDestinations(declared); err != nil {
		return err
	}

	for i, r := range bundle.GetRules() {
		for _, name := range r.GetTenants() {
			if _, ok := index[name]; !ok {
				return fmt.Errorf("rules[%d] (%s/%s) names the tenant %q, which tenants does not declare "+
					"(declared: %s)", i, r.GetPlatform(), r.GetDatasource(), name, strings.Join(tenantNames(declared), ", "))
			}
		}
	}
	return nil
}

func tenantNames(tenants []*pb_models.Tenant) []string {
	names := make([]string, 0, len(tenants))
	for _, t := range tenants {
		names = append(names, t.GetName())
	}
	return names
}

// checkTenantRepositories refuses one repository in two tenants. A push is the tenant of
// the repository it came from, so two owners would make that a guess.
func checkTenantRepositories(tenants []*pb_models.Tenant) error {
	for i, a := range tenants {
		for _, ra := range a.GetRepositories() {
			if strings.TrimSpace(ra) == "" {
				return fmt.Errorf("tenants[%d] (%s) lists an empty repository", i, a.GetName())
			}
			for _, b := range tenants[i+1:] {
				for _, rb := range b.GetRepositories() {
					if patternsOverlap(ra, rb) {
						return fmt.Errorf("repository %q of tenant %q and %q of tenant %q overlap: a push is the "+
							"tenant of the repository it came from, so no repository may belong to two tenants",
							ra, a.GetName(), rb, b.GetName())
					}
				}
			}
		}
	}
	return nil
}

// checkTenantSchemaRepos refuses one schema repository registered for two tenants: a push
// to it refreshes one tenant, and which one would be a guess.
func checkTenantSchemaRepos(tenants []*pb_models.Tenant) error {
	seen := map[string]string{}
	for _, t := range tenants {
		sr := t.GetSchemaRepo()
		if sr == nil {
			continue
		}
		repo := strings.ToLower(strings.TrimSuffix(strings.TrimSpace(sr.GetRepo()), ".git"))
		if repo == "" {
			return fmt.Errorf("tenant %q has a schema_repo without a repo", t.GetName())
		}
		if other, dup := seen[repo]; dup {
			return fmt.Errorf("schema repository %q is registered for tenants %q and %q: a push to it would "+
				"refresh one of them, so each tenant needs its own", sr.GetRepo(), other, t.GetName())
		}
		seen[repo] = t.GetName()
	}
	return nil
}

// checkTenantDestinations refuses two tenants that may both write one destination of one
// datasource, and a destination that names no datasource or allows no path.
func checkTenantDestinations(tenants []*pb_models.Tenant) error {
	for i, a := range tenants {
		for _, da := range a.GetDestinations() {
			if da.GetDatasource() == "" || len(da.GetPaths()) == 0 {
				return fmt.Errorf("tenant %q has a destination without a datasource or without paths", a.GetName())
			}
			for _, b := range tenants[i+1:] {
				for _, db := range b.GetDestinations() {
					if da.GetDatasource() != db.GetDatasource() {
						continue
					}
					for _, pa := range da.GetPaths() {
						for _, pb := range db.GetPaths() {
							if patternsOverlap(pa, pb) {
								return fmt.Errorf("datasource %q: %q of tenant %q and %q of tenant %q overlap, so both "+
									"could write the same place; give each tenant its own destinations",
									da.GetDatasource(), pa, a.GetName(), pb, b.GetName())
							}
						}
					}
				}
			}
		}
	}
	return nil
}

// patternsOverlap reports whether two glob patterns can name the same thing: they are the
// same, or one matches the other read as a literal. "*" matches anything, "/" included, as
// it does when the orchestrator checks a destination. Two different globs that happen to
// share a match ("a/*-x" and "a/y-*") are not seen; the point is to catch the mistakes that
// are made, an exact repeat or a name inside a wildcard.
func patternsOverlap(a, b string) bool {
	a, b = strings.ToLower(a), strings.ToLower(b)
	return a == b || globMatches(a, b) || globMatches(b, a)
}

// globMatches reports whether name matches pattern, where "*" is any run of characters and
// "?" is any one.
func globMatches(pattern, name string) bool {
	expr := regexp.QuoteMeta(pattern)
	expr = strings.ReplaceAll(expr, `\*`, `.*`)
	expr = strings.ReplaceAll(expr, `\?`, `.`)
	ok, _ := regexp.MatchString("^(?s:"+expr+")$", name)
	return ok
}
