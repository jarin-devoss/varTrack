package handlers_test

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"gateway-service/internal/middlewares"
	"gateway-service/internal/models"
	"gateway-service/internal/routes"
	pbm "vartrack/libs/gen/vartrack/v1/models"
	pbp "vartrack/libs/gen/vartrack/v1/models/platforms"
	pbu "vartrack/libs/gen/vartrack/v1/utils"
)

// twoGitHubs is two GitHub hosts, each with its own webhook secret, writing to
// the same datasource: their deliveries carry the same headers.
func twoGitHubs(publicSecret, enterpriseSecret string) *models.Bundle {
	github := func(tag, secret string) *pbm.Platform {
		g := &pbp.GitHub{Tag: str(tag), EventTypeHeader: "X-GitHub-Event", GitScmSignature: "X-Hub-Signature-256", PushEventName: "push"}
		if secret != "" {
			g.Secret = &pbu.SecretRef{Source: &pbu.SecretRef_Value{Value: secret}}
		}
		return &pbm.Platform{Config: &pbm.Platform_Github{Github: g}}
	}
	return models.NewBundle(&pbm.Bundle{
		Platforms: []*pbm.Platform{github("public", publicSecret), github("enterprise", enterpriseSecret)},
		Rules:     []*pbm.Rule{rule("github-public", "mongo"), rule("github-enterprise", "mongo")},
	})
}

func sign(secret, body string) string {
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(body))
	return "sha256=" + hex.EncodeToString(mac.Sum(nil))
}

// deliverSigned posts push to the datasource in production mode, where
// signatures are checked.
func deliverSigned(t *testing.T, bundle *models.Bundle, secret string) (*recorder, int, string) {
	t.Helper()
	orch := &recorder{}
	h := routes.WebhookRoutes(bundle, orch, middlewares.NewCircuitBreaker(middlewares.DefaultCircuitBreakerConfig()), nil, true)
	srv := httptest.NewServer(http.StripPrefix("/webhooks", h))
	defer srv.Close()

	req, _ := http.NewRequest(http.MethodPost, srv.URL+"/webhooks/mongo", strings.NewReader(push))
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-GitHub-Event", "push")
	req.Header.Set("X-GitHub-Delivery", "delivery-"+secret)
	if secret != "" {
		req.Header.Set("X-Hub-Signature-256", sign(secret, push))
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	defer resp.Body.Close()
	var body map[string]interface{}
	_ = json.NewDecoder(resp.Body).Decode(&body)
	detail, _ := json.Marshal(body)
	return orch, resp.StatusCode, string(detail)
}

func TestTwoPlatformsOfOneTypeAreToldApartBySecret(t *testing.T) {
	both := twoGitHubs("public-secret", "enterprise-secret")

	for secret, want := range map[string]string{
		"public-secret":     "github-public",
		"enterprise-secret": "github-enterprise",
	} {
		orch, status, detail := deliverSigned(t, both, secret)
		if status != http.StatusAccepted {
			t.Fatalf("signed with %s: status = %d (%s)", secret, status, detail)
		}
		if len(orch.forwarded) != 1 || orch.forwarded[0].GetPlatform() != want {
			t.Errorf("signed with %s: forwarded %v, want one webhook for %q", secret, orch.forwarded, want)
		}
		if orch.forwarded[0].GetRawPayload() != push {
			t.Errorf("the body was changed on the way: %q", orch.forwarded[0].GetRawPayload())
		}
	}
}

func TestADeliverySignedByNoPlatformIsRefusedAsUnsigned(t *testing.T) {
	orch, status, detail := deliverSigned(t, twoGitHubs("public-secret", "enterprise-secret"), "someone-elses")
	if status != http.StatusUnauthorized || !strings.Contains(detail, "invalid signature") {
		t.Errorf("status = %d (%s), want 401 invalid signature", status, detail)
	}
	if len(orch.forwarded) != 0 {
		t.Error("a webhook no secret signed was forwarded")
	}
}

func TestTheSameSecretOnTwoPlatformsStaysAmbiguous(t *testing.T) {
	orch, status, detail := deliverSigned(t, twoGitHubs("shared", "shared"), "shared")
	if status != http.StatusBadRequest || !strings.Contains(detail, "cannot be told apart") {
		t.Errorf("status = %d (%s), want 400 ambiguous", status, detail)
	}
	if len(orch.forwarded) != 0 {
		t.Error("an ambiguous webhook was forwarded")
	}
}

func TestPlatformsWithoutASecretStayAmbiguous(t *testing.T) {
	_, status, detail := deliverSigned(t, twoGitHubs("", ""), "")
	if status != http.StatusBadRequest || !strings.Contains(detail, "cannot be told apart") {
		t.Errorf("status = %d (%s), want 400 ambiguous", status, detail)
	}
}

func TestOnlyTheSecuredPlatformCanBePickedWhenTheOtherHasNoSecret(t *testing.T) {
	// A platform with no secret signs nothing, so a signed delivery is the secured one's.
	orch, status, detail := deliverSigned(t, twoGitHubs("public-secret", ""), "public-secret")
	if status != http.StatusAccepted || len(orch.forwarded) != 1 || orch.forwarded[0].GetPlatform() != "github-public" {
		t.Errorf("status = %d (%s), forwarded %v", status, detail, orch.forwarded)
	}
}
