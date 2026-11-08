package authz

import (
	"context"
	"errors"
	"fmt"
	"sort"
	"strings"

	"gateway-service/internal/models"
	pb "vartrack/libs/gen/vartrack/v1/models"
)

// DefaultTenant is the tenant of a bundle that declares none: it covers every repository.
const DefaultTenant = "default"

// DeniedByTenant: the caller may not act for the tenant they named.
const DeniedByTenant = "tenant"

// TenantSource is what the engine needs to know of the tenants an operator declared. The
// bundle implements it; a rule source that does not has no tenants, and one implicit tenant
// ("default") covers everything.
type TenantSource interface {
	Tenants() []*pb.Tenant
}

// AmbiguousTenantError: the caller may act for several tenants and named none.
type AmbiguousTenantError struct{ Tenants []string }

func (e *AmbiguousTenantError) Error() string {
	return "you can act for several tenants (" + strings.Join(e.Tenants, ", ") + "): name one with --tenant"
}

// declaredTenants are the tenants config.cue declares, none for a bundle without any.
func (e *Engine) declaredTenants() []*pb.Tenant {
	if src, ok := e.rules.(TenantSource); ok {
		return src.Tenants()
	}
	return nil
}

// tenantsOf are the declared tenants the rule allows: all of them when it names none.
func tenantsOf(declared []*pb.Tenant, rule Rule) []*pb.Tenant {
	if len(rule.Tenants) == 0 {
		return declared
	}
	var out []*pb.Tenant
	for _, t := range declared {
		for _, name := range rule.Tenants {
			if t.GetName() == name {
				out = append(out, t)
				break
			}
		}
	}
	return out
}

// tenantNames lists the names, in order.
func tenantNames(ts []*pb.Tenant) []string {
	names := make([]string, 0, len(ts))
	for _, t := range ts {
		names = append(names, t.GetName())
	}
	return names
}

// repositoriesOwned is whether any declared tenant's repositories cover a repository the
// rule names. When none does, the rule's repositories are nobody's in particular, and the
// rule's only tenant (if it names exactly one) stands for them.
func repositoriesOwned(declared []*pb.Tenant, rule Rule) bool {
	for _, t := range declared {
		for _, repo := range rule.Include {
			if matchesAny(repo, t.GetRepositories()) || patternsCross(repo, t.GetRepositories()) {
				return true
			}
		}
	}
	return false
}

// patternsCross is whether a rule's pattern ("acme/*") names repositories a tenant's patterns
// also name: the rule pattern is itself one of the tenant's, or a tenant pattern is inside it.
func patternsCross(repo string, patterns []string) bool {
	for _, p := range patterns {
		if strings.EqualFold(repo, p) || matchesAny(p, []string{repo}) {
			return true
		}
	}
	return false
}

// mayActFor is whether the caller may act for tenant in rule: an admin may; anyone else needs
// read access, in Git, to a repository both the tenant and the rule cover. It returns why not
// when they may not.
func (e *Engine) mayActFor(ctx context.Context, declared []*pb.Tenant, rule Rule, c *caller, t *pb.Tenant) (bool, string, error) {
	if c.admin {
		return true, "", nil
	}
	within := t.GetRepositories()
	if len(tenantsOf(declared, rule)) == 1 && !repositoriesOwned(declared, rule) {
		// the rule's one tenant stands for repositories no declared tenant owns: Git is asked
		// about the rule's repositories as they are
		within = nil
	} else if len(within) == 0 {
		return false, fmt.Sprintf("tenant %s has no repositories, so Git cannot say that %s may act for it",
			t.GetName(), c.login), nil
	}
	perm, err := e.rulePermissionIn(ctx, rule, c, within)
	if err != nil {
		return false, "", err
	}
	if perm.Read {
		return true, "", nil
	}
	return false, fmt.Sprintf("%s on %s has no access to the repositories tenant %s covers in the %s rule",
		c.login, rule.Platform, t.GetName(), rule.Datasource), nil
}

// tenantFor is the tenant a request acts for in rule, checked. requested is what the request
// named, "" when it named none.
//
//   - No tenants declared: the one implicit tenant, "default" (or the name asked for).
//   - One named: it must be declared, one the rule allows, and one the caller may act for.
//   - None named: the one tenant the caller may act for; several is an *AmbiguousTenantError.
//
// A tenant that is refused comes back as why (a denial), not as an error.
func (e *Engine) tenantFor(ctx context.Context, requested string, rule Rule, c *caller) (tenant, why string, err error) {
	declared := e.declaredTenants()
	if len(declared) == 0 {
		if requested == "" {
			return DefaultTenant, "", nil
		}
		return requested, "", nil
	}
	allowed := tenantsOf(declared, rule)
	if requested != "" {
		for _, t := range allowed {
			if t.GetName() != requested {
				continue
			}
			ok, why, err := e.mayActFor(ctx, declared, rule, c, t)
			if err != nil || !ok {
				return "", why, err
			}
			return requested, "", nil
		}
		return "", fmt.Sprintf("tenant %s is not one of the tenants of the %s rule (%s)", requested, rule.Datasource,
			strings.Join(tenantNames(allowed), ", ")), nil
	}
	var mine []string
	var lastWhy string
	for _, t := range allowed {
		ok, why, err := e.mayActFor(ctx, declared, rule, c, t)
		if err != nil {
			return "", "", err
		}
		if ok {
			mine = append(mine, t.GetName())
		} else {
			lastWhy = why
		}
	}
	switch len(mine) {
	case 0:
		if lastWhy == "" {
			lastWhy = fmt.Sprintf("the %s rule has no tenant", rule.Datasource)
		}
		return "", lastWhy, nil
	case 1:
		return mine[0], "", nil
	}
	sort.Strings(mine)
	return "", "", &AmbiguousTenantError{Tenants: mine}
}

// TenantsFor are the tenants the caller may act for in rule: every declared tenant the rule
// allows whose repositories, as far as the rule covers them, the caller can read in Git (an
// admin: all of them). A bundle that declares none has the one implicit tenant "default".
func (e *Engine) TenantsFor(ctx context.Context, ids []Identity, rule Rule) ([]string, error) {
	declared := e.declaredTenants()
	if len(declared) == 0 {
		return []string{DefaultTenant}, nil
	}
	c, err := e.callerFor(ctx, ids, rule)
	if err != nil {
		return nil, err
	}
	var out []string
	for _, t := range tenantsOf(declared, rule) {
		ok, _, err := e.mayActFor(ctx, declared, rule, c, t)
		if err != nil {
			return nil, err
		}
		if ok {
			out = append(out, t.GetName())
		}
	}
	return out, nil
}

// ResolveTenant is the checked tenant of a request that names no rule (listing tasks): the
// tenant named when the caller may act for it in at least one rule, else the one tenant they
// may act for, else an *AmbiguousTenantError. With no tenants declared it is the name asked
// for, or "default".
func (e *Engine) ResolveTenant(ctx context.Context, ids []Identity, requested string) (string, error) {
	declared := e.declaredTenants()
	if len(declared) == 0 {
		if requested == "" {
			return DefaultTenant, nil
		}
		return requested, nil
	}
	known := map[string]bool{}
	for _, t := range declared {
		known[t.GetName()] = true
	}
	if requested != "" && !known[requested] {
		return "", fmt.Errorf("tenant %s is not declared (declared: %s)", requested, strings.Join(tenantNames(declared), ", "))
	}
	seen := map[string]bool{}
	for _, rule := range e.BundleRules() {
		mine, err := e.TenantsFor(ctx, ids, rule)
		if err != nil {
			if isTokenRejected(err) {
				return "", err
			}
			continue // a platform that cannot be asked grants nothing
		}
		for _, name := range mine {
			seen[name] = true
		}
	}
	if requested != "" {
		if !seen[requested] {
			return "", fmt.Errorf("you cannot act for tenant %s: no access to the repositories it covers", requested)
		}
		return requested, nil
	}
	var names []string
	for name := range seen {
		names = append(names, name)
	}
	sort.Strings(names)
	switch len(names) {
	case 0:
		return "", fmt.Errorf("you cannot act for any tenant: no access to the repositories they cover")
	case 1:
		return names[0], nil
	}
	return "", &AmbiguousTenantError{Tenants: names}
}

func isTokenRejected(err error) bool { return err != nil && errors.Is(err, models.ErrTokenRejected) }

// tenantReach is whether a rule is within the caller's reach for tenant: they may act for it,
// or, when none is named, for at least one tenant of the rule. A bundle without declared
// tenants puts every rule in reach.
func (e *Engine) tenantReach(ctx context.Context, tenant string, rule Rule, c *caller) (bool, error) {
	_, why, err := e.tenantFor(ctx, tenant, rule, c)
	var ambiguous *AmbiguousTenantError
	switch {
	case errors.As(err, &ambiguous):
		return true, nil
	case err != nil:
		return false, err
	}
	return why == "", nil
}
