package bundle

import (
	"os"
	"path/filepath"
	"strings"
	"testing"

	pb_models "vartrack/libs/gen/vartrack/v1/models"
)

func TestCheckUniqueRules(t *testing.T) {
	rule := func(platform, datasource string) *pb_models.Rule {
		return &pb_models.Rule{Platform: platform, Datasource: datasource}
	}
	tests := []struct {
		name  string
		rules []*pb_models.Rule
		want  string // empty: accepted
	}{
		{"none", nil, ""},
		{"one", []*pb_models.Rule{rule("github", "mongo")}, ""},
		{"same datasource on two platforms", []*pb_models.Rule{rule("github", "mongo-primary"), rule("gitea-internal", "mongo-primary")}, ""},
		{"same platform, two datasources", []*pb_models.Rule{rule("github", "mongo-app"), rule("github", "mongo-db")}, ""},
		{"the same pair twice", []*pb_models.Rule{rule("github", "mongo"), rule("github", "redis"), rule("github", "mongo")},
			"rules[2] repeats github/mongo (rules[0])"},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			err := checkUniqueRules(&pb_models.Bundle{Rules: tc.rules})
			switch {
			case tc.want == "" && err != nil:
				t.Fatalf("unexpected error: %v", err)
			case tc.want != "" && (err == nil || !strings.Contains(err.Error(), tc.want)):
				t.Fatalf("error = %v, want it to mention %q", err, tc.want)
			}
		})
	}
}

func TestADuplicateRuleStopsTheBundleLoading(t *testing.T) {
	dir := t.TempDir()
	file := filepath.Join(dir, "config.cue")
	cue := `bundle: {
	rules: [
		{platform: "github", datasource: "mongo", file_name: "configs/app.yaml"},
		{platform: "github", datasource: "mongo", file_name: "configs/database.yaml"},
	]
}
`
	if err := os.WriteFile(file, []byte(cue), 0o600); err != nil {
		t.Fatal(err)
	}
	for _, strict := range []bool{true, false} { // the watcher refuses it too
		_, err := Load(file, Options{Strict: strict})
		if err == nil || !strings.Contains(err.Error(), "repeats github/mongo") || !strings.Contains(err.Error(), "mongo-db") {
			t.Fatalf("Load(strict=%v) error = %v, want the duplicate rule named with a way out", strict, err)
		}
	}
}
