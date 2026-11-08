package grants

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc/metadata"
)

const testSecret = "0123456789abcdef0123456789abcdef"

func fixedMinter(t *testing.T, at time.Time, ttl time.Duration) *Minter {
	t.Helper()
	m, err := NewMinter(testSecret, ttl)
	if err != nil {
		t.Fatal(err)
	}
	m.now = func() time.Time { return at }
	return m
}

func decode(t *testing.T, token string) (header, payload) {
	t.Helper()
	parts := strings.Split(token, ".")
	if len(parts) != 3 {
		t.Fatalf("want 3 segments, got %d", len(parts))
	}
	var h header
	var p payload
	for i, dst := range []any{&h, &p} {
		raw, err := base64.RawURLEncoding.DecodeString(parts[i])
		if err != nil {
			t.Fatal(err)
		}
		if err := json.Unmarshal(raw, dst); err != nil {
			t.Fatal(err)
		}
	}
	return h, p
}

func TestMintSignsAndCarriesClaims(t *testing.T) {
	at := time.Date(2030, 1, 2, 3, 4, 5, 0, time.UTC)
	tok, err := fixedMinter(t, at, 90*time.Second).Mint(Claims{
		Audience: AudienceSync, Subject: "alice@gitea", Tenant: "demo", Platform: "gitea", Datasource: "mongo",
	})
	if err != nil {
		t.Fatal(err)
	}
	h, p := decode(t, tok)
	if h.Alg != "HS256" || h.Typ != "JWT" {
		t.Fatalf("header = %+v", h)
	}
	want := payload{
		Issuer: Issuer, Audience: AudienceSync, Subject: "alice@gitea",
		IssuedAt: at.Unix(), NotBefore: at.Unix(), Expires: at.Add(90 * time.Second).Unix(),
		Tenant: "demo", Platform: "gitea", Datasource: "mongo",
	}
	if p != want {
		t.Fatalf("payload = %+v, want %+v", p, want)
	}

	parts := strings.Split(tok, ".")
	mac := hmac.New(sha256.New, []byte(testSecret))
	mac.Write([]byte(parts[0] + "." + parts[1]))
	if got := base64.RawURLEncoding.EncodeToString(mac.Sum(nil)); got != parts[2] {
		t.Fatalf("signature mismatch: %s != %s", got, parts[2])
	}
}

func TestMintOmitsEmptyClaims(t *testing.T) {
	tok, err := fixedMinter(t, time.Unix(1_900_000_000, 0), 0).Mint(Claims{Audience: AudienceWatch})
	if err != nil {
		t.Fatal(err)
	}
	raw, _ := base64.RawURLEncoding.DecodeString(strings.Split(tok, ".")[1])
	for _, k := range []string{"sub", "tenant", "platform", "datasource"} {
		if strings.Contains(string(raw), `"`+k+`"`) {
			t.Fatalf("empty claim %q was written: %s", k, raw)
		}
	}
	_, p := decode(t, tok)
	if p.Expires-p.IssuedAt != int64(DefaultTTL/time.Second) {
		t.Fatalf("default ttl not applied: %d", p.Expires-p.IssuedAt)
	}
}

func TestMintNeedsAudience(t *testing.T) {
	if _, err := fixedMinter(t, time.Now(), 0).Mint(Claims{Subject: "x"}); err == nil {
		t.Fatal("a grant with no audience must not be minted")
	}
}

func TestNewMinterRejectsShortSecret(t *testing.T) {
	if _, err := NewMinter("too-short", 0); err == nil {
		t.Fatal("a short secret must be refused")
	}
}

func TestLoadSecret(t *testing.T) {
	if got, _ := LoadSecret("  inline-value \n", ""); got != "inline-value" {
		t.Fatalf("inline = %q", got)
	}
	if got, err := LoadSecret("", ""); err != nil || got != "" {
		t.Fatalf("unset = %q, %v", got, err)
	}
	f := filepath.Join(t.TempDir(), "secret")
	if err := os.WriteFile(f, []byte("from-file\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if got, err := LoadSecret("", f); err != nil || got != "from-file" {
		t.Fatalf("file = %q, %v", got, err)
	}
	if _, err := LoadSecret("", filepath.Join(t.TempDir(), "missing")); err == nil {
		t.Fatal("a missing secret file must be an error, not an empty secret")
	}
	if got, _ := LoadSecret("inline", f); got != "inline" {
		t.Fatalf("inline must win over file, got %q", got)
	}
}

func TestWithBearerReplacesAuthorization(t *testing.T) {
	ctx := metadata.AppendToOutgoingContext(context.Background(), "authorization", "Bearer old", "x-other", "keep")
	ctx = WithBearer(ctx, "new-token")
	md, _ := metadata.FromOutgoingContext(ctx)
	if got := md.Get("authorization"); len(got) != 1 || got[0] != "Bearer new-token" {
		t.Fatalf("authorization = %v", got)
	}
	if got := md.Get("x-other"); len(got) != 1 || got[0] != "keep" {
		t.Fatalf("other metadata lost: %v", got)
	}
}

// TestInteropVector prints nothing and asserts nothing about the signature
// algorithm beyond the layout; the orchestrator's unit tests verify a token
// minted with exactly these inputs (see INTEROP_TOKEN in tests/unit/test_internal_auth.py).
func TestInteropVector(t *testing.T) {
	at := time.Unix(1_900_000_000, 0)
	tok, err := fixedMinter(t, at, 300*time.Second).Mint(Claims{
		Audience: AudienceSync, Subject: "alice@gitea", Tenant: "demo", Platform: "gitea", Datasource: "mongo",
	})
	if err != nil {
		t.Fatal(err)
	}
	const want = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9." // header segment never changes
	if !strings.HasPrefix(tok, want) {
		t.Fatalf("header segment changed: %s", tok)
	}
	t.Logf("INTEROP_TOKEN=%s", tok)
}

func TestVerifyAcceptsWhatMintSigned(t *testing.T) {
	at := time.Now()
	token, err := fixedMinter(t, at, 0).Mint(Claims{Audience: AudienceAuthz, Subject: "orchestrator", Tenant: "acme"})
	if err != nil {
		t.Fatal(err)
	}
	got, err := Verify(testSecret, token, AudienceAuthz, at)
	if err != nil {
		t.Fatal(err)
	}
	if got.Subject != "orchestrator" || got.Tenant != "acme" || got.Audience != AudienceAuthz {
		t.Errorf("claims = %+v", got.Claims)
	}
}

func TestVerifyRefusesWhatItShould(t *testing.T) {
	at := time.Now()
	good, _ := fixedMinter(t, at, 0).Mint(Claims{Audience: AudienceAuthz, Subject: "x"})
	other, _ := fixedMinter(t, at, 0).Mint(Claims{Audience: AudienceSync, Subject: "x"})
	stale, _ := fixedMinter(t, at.Add(-time.Hour), time.Minute).Mint(Claims{Audience: AudienceAuthz})
	early, _ := fixedMinter(t, at.Add(time.Hour), time.Minute).Mint(Claims{Audience: AudienceAuthz})
	long, _ := fixedMinter(t, at, time.Hour).Mint(Claims{Audience: AudienceAuthz})
	parts := strings.Split(good, ".")
	enc := base64.RawURLEncoding
	none := enc.EncodeToString([]byte(`{"alg":"none","typ":"JWT"}`)) + "." + parts[1] + "."

	for name, tc := range map[string]struct {
		secret, token string
	}{
		"wrong key":          {"another-secret-another-secret-0000", good},
		"no key":             {"", good},
		"wrong audience":     {testSecret, other},
		"expired":            {testSecret, stale},
		"not valid yet":      {testSecret, early},
		"too long a life":    {testSecret, long},
		"alg none":           {testSecret, none},
		"tampered claims":    {testSecret, parts[0] + "." + enc.EncodeToString([]byte(`{"iss":"vartrack","aud":"authz","iat":1,"nbf":1,"exp":9999999999}`)) + "." + parts[2]},
		"not three segments": {testSecret, "abc.def"},
		"empty":              {testSecret, ""},
	} {
		if _, err := Verify(tc.secret, tc.token, AudienceAuthz, at); !errors.Is(err, ErrInvalid) {
			t.Errorf("%s: err = %v, want ErrInvalid", name, err)
		}
	}
}

func TestBearerFromContext(t *testing.T) {
	ctx := metadata.NewIncomingContext(context.Background(), metadata.Pairs("authorization", "Bearer abc.def.ghi"))
	if got := BearerFromContext(ctx); got != "abc.def.ghi" {
		t.Errorf("got %q", got)
	}
	if got := BearerFromContext(context.Background()); got != "" {
		t.Errorf("no metadata: got %q", got)
	}
	ctx = metadata.NewIncomingContext(context.Background(), metadata.Pairs("authorization", "Basic abc"))
	if got := BearerFromContext(ctx); got != "" {
		t.Errorf("basic: got %q", got)
	}
}
