package platforms

import (
	"testing"

	pb_gt "gateway-service/internal/gen/proto/go/vartrack/v1/models/platforms"
)

func TestGiteaWebhookHeadersAndEventsComeFromTheProto(t *testing.T) {
	// A bundle like docs/examples/gitea.md sets none of the fixed fields;
	// gitea.proto pins them as buf.validate string consts.
	g := &Gitea{config: &pb_gt.Gitea{Endpoint: "http://gitea:3000", Protocol: "http"}}

	if got := g.EventTypeHeader(); got != "X-Gitea-Event" {
		t.Errorf("EventTypeHeader() = %q, want X-Gitea-Event", got)
	}
	if got := g.SignatureHeader(); got != "X-Gitea-Signature" {
		t.Errorf("SignatureHeader() = %q, want X-Gitea-Signature", got)
	}
	if !g.IsPushEvent("push") || g.IsPushEvent("pull_request") {
		t.Error("IsPushEvent should accept only push")
	}
	if !g.IsPREvent("pull_request") || g.IsPREvent("push") {
		t.Error("IsPREvent should accept only pull_request")
	}
}
