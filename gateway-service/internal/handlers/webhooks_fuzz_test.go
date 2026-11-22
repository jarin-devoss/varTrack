package handlers

import (
	"strings"
	"testing"
)

// The body of a webhook is whatever its sender wrote, and the gateway reads it before it has checked
// anything else about it. These run the readers on arbitrary bytes: they must answer, never panic.
//
//	go test -run '^$' -fuzz '^FuzzValidateWebhookStream$' -fuzztime 30s ./internal/handlers

func FuzzValidateWebhookStream(f *testing.F) {
	f.Add("github", "push", `{"ref":"refs/heads/main","repository":{"full_name":"acme/configs"}}`)
	f.Add("gitea-team", "pull_request", `{"action":"opened","pull_request":{},"repository":{}}`)
	f.Add("gitlab", "Push Hook", `{"ref":"refs/heads/main","project":{}}`)
	f.Add("bitbucket", "repo:push", `{"push":{},"repository":{}}`)
	f.Add("slack", "", `{"type":"event_callback"}`)
	f.Add("github", "push", ``)
	f.Add("github", "push", `{"ref":`)
	f.Add("", "", `null`)
	f.Add("github", "push", `[1,2,3]`)
	f.Add("github", "push", `{"ref":{"a":{"b":{"c":[[[[]]]]}}}}`)

	f.Fuzz(func(t *testing.T, platform, event, body string) {
		_ = validateWebhookStream(platform, event, strings.NewReader(body))
	})
}

func FuzzExtractRepoBranch(f *testing.F) {
	f.Add("github", []byte(`{"ref":"refs/heads/main","repository":{"full_name":"acme/configs"}}`))
	f.Add("gitlab", []byte(`{"ref":"refs/heads/dev","project":{"path_with_namespace":"acme/configs"}}`))
	f.Add("bitbucket", []byte(`{"push":{"changes":[{"new":{"name":"main"}}]},"repository":{"full_name":"acme/configs"}}`))
	f.Add("github", []byte(`{"push":{"changes":[]}}`))
	f.Add("", []byte(`not json`))

	f.Fuzz(func(t *testing.T, platform string, body []byte) {
		_, _ = extractRepoBranch(platform, body)
	})
}

// A platform's name is its type, or its type and a tag: the type has no hyphen, whatever the tag is.
func FuzzPlatformType(f *testing.F) {
	for _, s := range []string{"github", "gitea-team", "gitea-a-b", "", "-", "--x", "x-"} {
		f.Add(s)
	}
	f.Fuzz(func(t *testing.T, name string) {
		got := platformType(name)
		if strings.Contains(got, "-") {
			t.Errorf("platformType(%q) = %q, which has a hyphen", name, got)
		}
		if !strings.HasPrefix(name, got) {
			t.Errorf("platformType(%q) = %q, which is not a prefix of it", name, got)
		}
	})
}
