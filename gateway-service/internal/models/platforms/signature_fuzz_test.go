package platforms

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"testing"

	pb_gt "vartrack/libs/gen/vartrack/v1/models/platforms"
)

// A webhook's signature header is the sender's to write. It verifies when it is the right digest of the body under
// the platform's secret, and for no other text: not another case of the same digest, not a prefix of it, not
// the digest of another body.
//
//	go test -run '^$' -fuzz '^FuzzGitHubVerifySignature$' -fuzztime 30s ./internal/models/platforms

const fuzzSecret = "a-webhook-secret"

func digest(body []byte) string {
	mac := hmac.New(sha256.New, []byte(fuzzSecret))
	mac.Write(body)
	return hex.EncodeToString(mac.Sum(nil))
}

func FuzzGitHubVerifySignature(f *testing.F) {
	g := &GitHub{config: &pb_gt.GitHub{}, secret: fuzzSecret}
	body := []byte(`{"ref":"refs/heads/main"}`)
	f.Add(body, "sha256="+digest(body))
	f.Add(body, digest(body))
	f.Add(body, "sha256=")
	f.Add(body, "")
	f.Add(body, "sha256="+digest([]byte("another body")))
	f.Add([]byte{}, "SHA256="+digest(nil))

	f.Fuzz(func(t *testing.T, body []byte, header string) {
		mac := g.WebhookHasher()
		mac.Write(body)
		got := g.VerifySignature(mac, header)
		want := header == "sha256="+digest(body)
		if got != want {
			t.Errorf("VerifySignature(header %q) = %v, want %v", header, got, want)
		}
	})
}

func FuzzGiteaVerifySignature(f *testing.F) {
	g := &Gitea{config: &pb_gt.Gitea{}, secret: fuzzSecret}
	body := []byte(`{"ref":"refs/heads/main"}`)
	f.Add(body, digest(body))
	f.Add(body, "sha256="+digest(body))
	f.Add(body, "")
	f.Add(body, digest([]byte("another body")))

	f.Fuzz(func(t *testing.T, body []byte, header string) {
		mac := g.WebhookHasher()
		mac.Write(body)
		got := g.VerifySignature(mac, header)
		want := header == digest(body)
		if got != want {
			t.Errorf("VerifySignature(header %q) = %v, want %v", header, got, want)
		}
	})
}
