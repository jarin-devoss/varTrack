package handlers

import (
	"strings"
	"testing"
)

func TestATaggedPlatformIsHandledLikeItsType(t *testing.T) {
	for name, want := range map[string]string{"gitea": "gitea", "gitea-team": "gitea", "github-dr": "github", "github": "github"} {
		if got := platformType(name); got != want {
			t.Errorf("platformType(%q) = %q, want %q", name, got, want)
		}
	}

	push := `{"ref":"refs/heads/main","repository":{"full_name":"payments/payments-config"}}`
	if err := validateWebhookStream("gitea-team", "push", strings.NewReader(push)); err != nil {
		t.Errorf("a push from a second Gitea server was refused: %v", err)
	}
	if repo, branch := extractRepoBranch("gitea-team", []byte(push)); repo != "payments/payments-config" || branch != "main" {
		t.Errorf("extractRepoBranch = %q, %q", repo, branch)
	}
	if err := validateWebhookStream("gitea-team", "push", strings.NewReader(`{}`)); err == nil {
		t.Error("an empty push from a second Gitea server passed structural validation")
	}
}
