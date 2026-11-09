// Package grants mints the short-lived signed credentials that varTrack's
// services present to the orchestrator.
//
// The orchestrator does not trust where a call comes from; it trusts what the
// call carries. A grant is a compact HS256-signed token (header.payload.sig,
// base64url, the usual JWT layout) that names who is asking and what the call
// is allowed to touch:
//
//	aud        which API the grant is for: "sync", "watch", "admin" or "authz"
//	sub        the caller ("login@platform" for a CLI user, a service name otherwise)
//	tenant     the tenant the call is scoped to (optional)
//	platform   the Git platform the call was authorised on (optional)
//	datasource the datasource the call was authorised for (optional)
//	iat/nbf/exp issue, not-before and expiry times
//
// The orchestrator checks the signature, the audience and the lifetime on
// every call, and acts only inside the claims. The same layout is verified by
// the orchestrator's app/api/tokens.py; the gateway verifies the "authz" grants
// the orchestrator presents to its permission engine with Verify.
package grants

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"errors"
	"fmt"
	"os"
	"strings"
	"time"

	"google.golang.org/grpc/metadata"
)

// The audiences the orchestrator accepts.
const (
	AudienceSync  = "sync"  // webhook and CLI calls the gateway forwards
	AudienceWatch = "watch" // the watcher's self-heal calls
	AudienceAdmin = "admin" // the orchestrator's admin listener
	AudienceAuthz = "authz" // the orchestrator's questions to the gateway's permission engine

	// Issuer is the fixed "iss" claim of every grant.
	Issuer = "vartrack"

	// MinSecretLength is the shortest signing secret accepted.
	MinSecretLength = 32

	// DefaultTTL is how long a minted grant lives unless the minter says otherwise.
	DefaultTTL = 60 * time.Second

	// Leeway is the clock difference between two hosts that Verify forgives.
	Leeway = 30 * time.Second
)

// maxLifetime is the longest a grant may live, from issue to expiry, per audience.
var maxLifetime = map[string]time.Duration{
	AudienceSync:  10 * time.Minute,
	AudienceWatch: 10 * time.Minute,
	AudienceAuthz: 10 * time.Minute,
	AudienceAdmin: 30 * 24 * time.Hour,
}

// Claims is what a grant says. Empty fields are left out of the token.
type Claims struct {
	Audience   string
	Subject    string
	Tenant     string
	Platform   string
	Datasource string
}

// Issued is what a verified grant says, besides the Claims.
type Issued struct {
	Claims
	IssuedAt time.Time
	Expires  time.Time
}

type header struct {
	Alg string `json:"alg"`
	Typ string `json:"typ"`
}

type payload struct {
	Issuer     string `json:"iss"`
	Audience   string `json:"aud"`
	Subject    string `json:"sub,omitempty"`
	IssuedAt   int64  `json:"iat"`
	NotBefore  int64  `json:"nbf"`
	Expires    int64  `json:"exp"`
	Tenant     string `json:"tenant,omitempty"`
	Platform   string `json:"platform,omitempty"`
	Datasource string `json:"datasource,omitempty"`
}

// Minter signs grants with one shared secret.
type Minter struct {
	key []byte
	ttl time.Duration
	now func() time.Time
}

// NewMinter returns a Minter for secret. A grant it mints lives for ttl
// (DefaultTTL when ttl is zero or negative).
func NewMinter(secret string, ttl time.Duration) (*Minter, error) {
	if len(secret) < MinSecretLength {
		return nil, fmt.Errorf("grants: the signing secret must be at least %d characters", MinSecretLength)
	}
	if ttl <= 0 {
		ttl = DefaultTTL
	}
	return &Minter{key: []byte(secret), ttl: ttl, now: time.Now}, nil
}

// Mint returns a signed grant for c.
func (m *Minter) Mint(c Claims) (string, error) {
	if m == nil || len(m.key) == 0 {
		return "", errors.New("grants: the minter has no signing secret")
	}
	if c.Audience == "" {
		return "", errors.New("grants: a grant needs an audience")
	}
	clock := m.now
	if clock == nil {
		clock = time.Now
	}
	ttl := m.ttl
	if ttl <= 0 {
		ttl = DefaultTTL
	}
	now := clock().UTC()
	return sign(m.key, payload{
		Issuer:     Issuer,
		Audience:   c.Audience,
		Subject:    c.Subject,
		IssuedAt:   now.Unix(),
		NotBefore:  now.Unix(),
		Expires:    now.Add(ttl).Unix(),
		Tenant:     c.Tenant,
		Platform:   c.Platform,
		Datasource: c.Datasource,
	})
}

func sign(key []byte, p payload) (string, error) {
	h, err := json.Marshal(header{Alg: "HS256", Typ: "JWT"})
	if err != nil {
		return "", err
	}
	body, err := json.Marshal(p)
	if err != nil {
		return "", err
	}
	enc := base64.RawURLEncoding
	signingInput := enc.EncodeToString(h) + "." + enc.EncodeToString(body)
	mac := hmac.New(sha256.New, key)
	mac.Write([]byte(signingInput))
	return signingInput + "." + enc.EncodeToString(mac.Sum(nil)), nil
}

// ErrInvalid is wrapped by every error Verify returns for a grant that must not
// be honoured. Its text is safe to return to the caller: it never includes the
// token or the key.
var ErrInvalid = errors.New("invalid grant")

func invalid(why string) error { return fmt.Errorf("%w: %s", ErrInvalid, why) }

// Verify checks token against secret and audience and returns what it says. The
// signature is checked first, in constant time, and only a token the right key
// signed has its claims read; whatever the header says, only HS256 is accepted.
// now is the time to judge the lifetime by (the current time when zero).
func Verify(secret, token, audience string, now time.Time) (Issued, error) {
	if secret == "" {
		return Issued{}, invalid("no key is configured for this audience")
	}
	parts := strings.Split(token, ".")
	if len(parts) != 3 || parts[0] == "" || parts[1] == "" || parts[2] == "" {
		return Issued{}, invalid("malformed token")
	}
	enc := base64.RawURLEncoding
	rawHeader, err := enc.DecodeString(parts[0])
	if err != nil {
		return Issued{}, invalid("malformed token")
	}
	var h header
	if err := json.Unmarshal(rawHeader, &h); err != nil {
		return Issued{}, invalid("malformed token")
	}
	if h.Alg != "HS256" {
		return Issued{}, invalid("unsupported token algorithm")
	}
	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(parts[0] + "." + parts[1]))
	given, err := enc.DecodeString(parts[2])
	if err != nil || !hmac.Equal(mac.Sum(nil), given) {
		return Issued{}, invalid("invalid signature")
	}
	rawBody, err := enc.DecodeString(parts[1])
	if err != nil {
		return Issued{}, invalid("malformed token")
	}
	var p payload
	if err := json.Unmarshal(rawBody, &p); err != nil {
		return Issued{}, invalid("malformed token")
	}
	if p.Issuer != Issuer {
		return Issued{}, invalid("wrong issuer")
	}
	if p.Audience != audience {
		return Issued{}, invalid("wrong audience")
	}
	if p.IssuedAt == 0 || p.NotBefore == 0 || p.Expires == 0 {
		return Issued{}, invalid("the token must carry iat, nbf and exp")
	}
	if now.IsZero() {
		now = time.Now()
	}
	issued, expires := time.Unix(p.IssuedAt, 0), time.Unix(p.Expires, 0)
	if now.Before(time.Unix(p.NotBefore, 0).Add(-Leeway)) || now.Before(issued.Add(-Leeway)) {
		return Issued{}, invalid("token is not valid yet")
	}
	if now.After(expires.Add(Leeway)) {
		return Issued{}, invalid("token has expired")
	}
	limit, known := maxLifetime[audience]
	if !known {
		return Issued{}, invalid("unknown audience")
	}
	if expires.Sub(issued) > limit {
		return Issued{}, invalid("token lifetime is longer than this audience allows")
	}
	return Issued{
		Claims: Claims{
			Audience: p.Audience, Subject: p.Subject, Tenant: p.Tenant,
			Platform: p.Platform, Datasource: p.Datasource,
		},
		IssuedAt: issued,
		Expires:  expires,
	}, nil
}

// BearerFromContext is the token of the incoming "authorization: Bearer <token>"
// metadata, or "".
func BearerFromContext(ctx context.Context) string {
	md, ok := metadata.FromIncomingContext(ctx)
	if !ok {
		return ""
	}
	for _, v := range md.Get("authorization") {
		scheme, rest, found := strings.Cut(strings.TrimSpace(v), " ")
		if found && strings.EqualFold(scheme, "bearer") {
			return strings.TrimSpace(rest)
		}
	}
	return ""
}

// LoadSecret returns the signing secret: the value itself, else the contents
// of the file named by file (trailing whitespace removed). Both empty means no
// secret is configured and "" is returned.
func LoadSecret(value, file string) (string, error) {
	if v := strings.TrimSpace(value); v != "" {
		return v, nil
	}
	if file == "" {
		return "", nil
	}
	raw, err := os.ReadFile(file)
	if err != nil {
		return "", fmt.Errorf("grants: read signing secret file: %w", err)
	}
	return strings.TrimSpace(string(raw)), nil
}

// WithBearer returns ctx with grant attached as the outgoing "authorization"
// metadata, replacing any authorization already there.
func WithBearer(ctx context.Context, grant string) context.Context {
	md, _ := metadata.FromOutgoingContext(ctx)
	md = md.Copy()
	md.Set("authorization", "Bearer "+grant)
	return metadata.NewOutgoingContext(ctx, md)
}
