package platforms

import (
	"context"
	"testing"

	pb_models "vartrack/libs/gen/vartrack/v1/models"
	pb_gt "vartrack/libs/gen/vartrack/v1/models/platforms"
	pb_utils "vartrack/libs/gen/vartrack/v1/utils"
	"gateway-service/internal/models"
)

type inlineResolver struct{}

func (inlineResolver) Resolve(_ context.Context, ref *pb_utils.SecretRef, _ string) (string, error) {
	return ref.GetValue(), nil
}

func TestATaggedPlatformOpensTheDriverOfItsType(t *testing.T) {
	// A second Gitea server, "gitea-team", is still a Gitea: the factory must
	// open the "gitea" driver, not look for one named "gitea-team".
	tag := "team"
	cfg := &pb_models.Platform{Config: &pb_models.Platform_Gitea{Gitea: &pb_gt.Gitea{
		Tag: &tag, Endpoint: "http://gitea-team:3000", Protocol: "http",
	}}}
	if got := models.PlatformName(cfg); got != "gitea-team" {
		t.Fatalf("PlatformName = %q, want gitea-team", got)
	}
	plat, err := models.PlatformFactory.Get(context.Background(), models.PlatformConfig{Platform: cfg, Resolver: inlineResolver{}})
	if err != nil {
		t.Fatalf("PlatformFactory.Get: %v", err)
	}
	if got := plat.EventTypeHeader(); got != "X-Gitea-Event" {
		t.Errorf("opened %T with EventTypeHeader %q, want a Gitea platform", plat, got)
	}
}

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
