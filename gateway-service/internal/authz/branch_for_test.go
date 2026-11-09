package authz

import (
	"encoding/json"
	"os"
	"testing"
)

// branchForRow is one case of testdata/branch_for.json. The orchestrator's tests read the same file and check, with
// the ETL's own environment resolver, that the branch named here is a branch whose files land in that environment.
type branchForRow struct {
	Name string `json:"name"`
	Rule struct {
		BranchMap    map[string]string `json:"branch_map"`
		BranchAsEnv  bool              `json:"branch_as_env"`
		Branch       string            `json:"branch"`
		FilePathEnvs []string          `json:"file_path_envs"`
	} `json:"rule"`
	Env      string `json:"env"`
	Branch   string `json:"branch"`
	FilePath string `json:"file_path"`
}

func TestBranchForFollowsTheEnvironmentResolver(t *testing.T) {
	raw, err := os.ReadFile("testdata/branch_for.json")
	if err != nil {
		t.Fatal(err)
	}
	var rows []branchForRow
	if err := json.Unmarshal(raw, &rows); err != nil {
		t.Fatal(err)
	}
	for _, r := range rows {
		t.Run(r.Name, func(t *testing.T) {
			rule := Rule{
				BranchMap:    r.Rule.BranchMap,
				BranchAsEnv:  r.Rule.BranchAsEnv,
				Branch:       r.Rule.Branch,
				FilePathEnvs: r.Rule.FilePathEnvs,
			}
			if got := branchFor(rule, r.Env); got != r.Branch {
				t.Errorf("branchFor(%s) = %q, want %q", r.Env, got, r.Branch)
			}
		})
	}
}
