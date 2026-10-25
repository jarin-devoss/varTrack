// Package watcher runs the self-heal loop: every poll it asks the orchestrator
// whether each self_heal rule's datasource still matches Git, and asks it to
// re-sync the ones that drifted.
//
// The watcher reads no sink and holds no sink credentials, and it reads no
// rule either. The orchestrator — which writes the sinks and is the one
// service that layers config.cue, the tenants' bundle.json and the
// repositories' vartrack.json into rules — says which rules to watch
// (Watcher.ListWatchTargets), reads the sinks and compares them, key by key,
// with the state it last synced from Git (Watcher.CheckDatasource). The
// watcher holds no sink connections and no copy of the rules: it asks for the
// work instead of reading it from configuration.
package watcher

import (
	"context"

	"watcher-service/internal/healer"
)

// Orchestrator is what the watcher asks: which rules to watch (ListTargets),
// is this datasource still in sync with Git (Check), and if not, sync it
// again (Heal). *healer.Healer is one.
type Orchestrator interface {
	ListTargets(ctx context.Context) ([]healer.WatchTarget, error)
	Check(ctx context.Context, req healer.HealRequest) (healer.GitState, string, error)
	Heal(ctx context.Context, req healer.HealRequest) error
}

// Target is one self_heal rule of one tenant the watcher keeps in sync.
type Target struct {
	// Name is the rule, "{platform}/{datasource}".
	Name string
	// Tenant is the tenant whose rule it is.
	Tenant string
	// Request names the datasource in Check and Heal.
	Request healer.HealRequest
}

// NewTarget is the Target for a rule the orchestrator listed.
func NewTarget(t healer.WatchTarget) Target {
	return Target{
		Name:   t.Platform + "/" + t.Datasource,
		Tenant: t.Tenant,
		Request: healer.HealRequest{
			Datasource: t.Datasource,
			Platform:   t.Platform,
			Tenant:     t.Tenant,
		},
	}
}

// Key identifies the target: two tenants may have a rule for the same
// platform and datasource.
func (t Target) Key() string {
	return t.Tenant + "|" + t.Name
}
