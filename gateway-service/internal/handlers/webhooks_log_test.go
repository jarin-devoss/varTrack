package handlers

import (
	"bytes"
	"hash"
	"log/slog"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"

	"gateway-service/internal/models"
)

// eventPlatform is a Platform that knows two event names.
type eventPlatform struct{ models.Platform }

func (eventPlatform) EventTypeHeader() string                { return "X-Test-Event" }
func (eventPlatform) SignatureHeader() string                { return "X-Test-Signature" }
func (eventPlatform) IsPushEvent(et string) bool             { return et == "push" }
func (eventPlatform) IsPREvent(et string) bool               { return et == "pull_request" }
func (eventPlatform) Secret() string                         { return "" }
func (eventPlatform) WebhookHasher() hash.Hash               { return nil }
func (eventPlatform) VerifySignature(hash.Hash, string) bool { return true }

func TestKnownEventTypeIsOneOfThreeNames(t *testing.T) {
	p := eventPlatform{}
	for in, want := range map[string]string{
		"push":                     "push",
		"pull_request":             "pull_request",
		"ping":                     "other",
		"":                         "other",
		"push\nlevel=ERROR forged": "other",
	} {
		if got := knownEventType(p, in); got != want {
			t.Errorf("knownEventType(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestARawEventTypeHeaderIsNotLogged(t *testing.T) {
	var logs bytes.Buffer
	old := slog.Default()
	slog.SetDefault(slog.New(slog.NewJSONHandler(&logs, nil)))
	defer slog.SetDefault(old)

	crafted := "push-but-crafted-4f9a"
	h := &WebhookHandler{bundleService: &models.Bundle{}, skipValidation: true}
	req := httptest.NewRequest(http.MethodPost, "/webhook", strings.NewReader(`not json`))
	req.Header.Set("X-Test-Event", crafted)
	rec := httptest.NewRecorder()

	if _, _, ok := h.verifyWebhook(rec, req, eventPlatform{}, "test", "cid", "rid"); ok {
		t.Fatal("a body that is not JSON passed structural validation")
	}
	out := logs.String()
	if !strings.Contains(out, "failed structural validation") || !strings.Contains(out, `"event_type":"other"`) {
		t.Fatalf("expected the validation warning with event_type other, got: %s", out)
	}
	if strings.Contains(out, crafted) {
		t.Fatalf("the event type header's value was logged: %s", out)
	}
}
