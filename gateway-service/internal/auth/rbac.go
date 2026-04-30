package auth

import (
	"fmt"
	"strings"
	"sync"

	"github.com/casbin/casbin/v2"
	casbinmodel "github.com/casbin/casbin/v2/model"
	"github.com/casbin/casbin/v2/persist"
)

// RBAC model — subject/role · resource · action.
//
// Roles are hierarchical: a group membership (e.g. "ops-team") maps to a
// role (e.g. "role:operator") which maps to a set of permissions.
//
// Policy CSV format (identical to the rule syntax):
//
//	# permissions
//	p, role:admin,    *,          *,         allow
//	p, role:operator, datasource, sync,      allow
//	p, role:operator, datasource, validate,  allow
//	p, role:operator, task,       get,       allow
//	p, role:operator, task,       list,      allow
//	p, role:viewer,   datasource, validate,  allow
//	p, role:viewer,   task,       get,       allow
//	p, role:viewer,   task,       list,      allow
//	# role assignments
//	g, alice@example.com, role:admin
//	g, ops-team,          role:operator
const defaultPolicy = `
p, role:admin,    *,          *,         allow
p, role:operator, datasource, sync,      allow
p, role:operator, datasource, validate,  allow
p, role:operator, task,       get,       allow
p, role:operator, task,       list,      allow
p, role:viewer,   datasource, validate,  allow
p, role:viewer,   task,       get,       allow
p, role:viewer,   task,       list,      allow
`

// casbinModel is the Casbin model definition for (subject, resource, action) → effect.
const casbinModel = `
[request_definition]
r = sub, obj, act

[policy_definition]
p = sub, obj, act, eft

[role_definition]
g = _, _

[policy_effect]
e = some(where (p.eft == allow))

[matchers]
m = g(r.sub, p.sub) && (p.obj == "*" || r.obj == p.obj) && (p.act == "*" || r.act == p.act)
`

// noopAdapter is a minimal Casbin adapter that starts with an empty policy.
// Policies are loaded programmatically via AddPolicy / AddGroupingPolicy.
type noopAdapter struct{}

func (noopAdapter) LoadPolicy(casbinmodel.Model) error          { return nil }
func (noopAdapter) SavePolicy(casbinmodel.Model) error          { return nil }
func (noopAdapter) AddPolicy(string, string, []string) error    { return nil }
func (noopAdapter) RemovePolicy(string, string, []string) error { return nil }
func (noopAdapter) RemoveFilteredPolicy(string, string, int, ...string) error {
	return nil
}

// ensure noopAdapter satisfies the interface at compile time.
var _ persist.Adapter = noopAdapter{}

// Enforcer wraps the Casbin enforcer with a default-deny policy.
type Enforcer struct {
	mu       sync.RWMutex
	enforcer *casbin.Enforcer
}

// NewEnforcer creates an Enforcer from a policy string in CSV format.
// When policy is empty the built-in default policy is used.
func NewEnforcer(policyCSV string) (*Enforcer, error) {
	if strings.TrimSpace(policyCSV) == "" {
		policyCSV = defaultPolicy
	}

	m, err := casbinmodel.NewModelFromString(casbinModel)
	if err != nil {
		return nil, fmt.Errorf("casbin model: %w", err)
	}

	e, err := casbin.NewEnforcer(m, noopAdapter{})
	if err != nil {
		return nil, fmt.Errorf("casbin enforcer: %w", err)
	}

	if err := loadPolicyCSV(e, policyCSV); err != nil {
		return nil, fmt.Errorf("load policy: %w", err)
	}

	return &Enforcer{enforcer: e}, nil
}

// Allow returns true when the subject (or any of its groups) is permitted to
// perform action on resource.
func (e *Enforcer) Allow(subject string, groups []string, resource, action string) bool {
	e.mu.RLock()
	defer e.mu.RUnlock()

	// Check the subject directly.
	if ok, _ := e.enforcer.Enforce(subject, resource, action); ok {
		return true
	}

	// Check each group membership the subject belongs to.
	for _, g := range groups {
		if ok, _ := e.enforcer.Enforce(g, resource, action); ok {
			return true
		}
	}
	return false
}

// Reload replaces the current policy with a new CSV string.
// Useful for runtime policy updates without restarting the gateway.
func (e *Enforcer) Reload(policyCSV string) error {
	m, err := casbinmodel.NewModelFromString(casbinModel)
	if err != nil {
		return err
	}

	newEnf, err := casbin.NewEnforcer(m, noopAdapter{})
	if err != nil {
		return err
	}
	if err := loadPolicyCSV(newEnf, policyCSV); err != nil {
		return err
	}

	e.mu.Lock()
	e.enforcer = newEnf
	e.mu.Unlock()
	return nil
}

// loadPolicyCSV loads a Casbin policy from a multi-line CSV string.
// Lines starting with '#' or empty lines are ignored.
func loadPolicyCSV(e *casbin.Enforcer, csv string) error {
	for _, line := range strings.Split(csv, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		parts := splitCSV(line)
		if len(parts) < 2 {
			continue
		}
		pType := strings.TrimSpace(parts[0])
		vals  := parts[1:]
		for i := range vals {
			vals[i] = strings.TrimSpace(vals[i])
		}
		switch pType {
		case "p":
			if _, err := e.AddPolicy(vals); err != nil {
				return fmt.Errorf("add policy %q: %w", line, err)
			}
		case "g":
			if _, err := e.AddGroupingPolicy(vals); err != nil {
				return fmt.Errorf("add grouping policy %q: %w", line, err)
			}
		}
	}
	return nil
}

func splitCSV(line string) []string {
	parts := strings.Split(line, ",")
	for i := range parts {
		parts[i] = strings.TrimSpace(parts[i])
	}
	return parts
}
