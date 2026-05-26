// Package auth provides JWT validation and RBAC enforcement for CLI requests.
package auth

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"

	"github.com/go-jose/go-jose/v4"
	josejwt "github.com/go-jose/go-jose/v4/jwt"
)

// Claims holds the validated JWT payload fields used for RBAC.
type Claims struct {
	Subject string
	Email   string
	Name    string
	Groups  []string
	Issuer  string
}

// allowedAlgorithms lists the asymmetric signing algorithms accepted for
// token verification. Symmetric algorithms (HS*) are excluded because they
// require sharing the signing secret with this service, which is not the
// OIDC model.
var allowedAlgorithms = []jose.SignatureAlgorithm{
	jose.RS256, jose.RS384, jose.RS512,
	jose.ES256, jose.ES384, jose.ES512,
	jose.PS256, jose.PS384, jose.PS512,
}

// JWTValidator validates Bearer tokens against an OIDC provider's JWKS.
// The JWKS is cached and re-fetched every 12 hours.
type JWTValidator struct {
	issuerURL  string
	audience   string
	fetchEvery time.Duration

	mu        sync.RWMutex
	keySet    jose.JSONWebKeySet
	lastFetch time.Time
}

// NewJWTValidator creates a JWTValidator for the given OIDC issuer.
// audience is the expected "aud" claim value (typically the client ID).
// Pass an empty audience to skip audience validation.
func NewJWTValidator(issuerURL, audience string) (*JWTValidator, error) {
	v := &JWTValidator{
		issuerURL:  strings.TrimRight(issuerURL, "/"),
		audience:   audience,
		fetchEvery: 12 * time.Hour,
	}
	if err := v.refreshJWKS(context.Background()); err != nil {
		return nil, fmt.Errorf("initial JWKS fetch from %s: %w", issuerURL, err)
	}
	return v, nil
}

// Validate verifies the JWT signature, expiry, issuer, and audience.
// It returns the parsed Claims on success.
func (v *JWTValidator) Validate(ctx context.Context, rawToken string) (*Claims, error) {
	v.mu.RLock()
	stale := time.Since(v.lastFetch) > v.fetchEvery
	v.mu.RUnlock()
	if stale {
		// Refresh best-effort; proceed with cached keys on failure.
		_ = v.refreshJWKS(ctx)
	}

	return v.parseClaims(rawToken)
}

// parseClaims verifies the JWT signature against the cached JWKS, then
// validates the standard time and issuer/audience claims.
func (v *JWTValidator) parseClaims(rawToken string) (*Claims, error) {
	tok, err := josejwt.ParseSigned(rawToken, allowedAlgorithms)
	if err != nil {
		return nil, fmt.Errorf("parse JWT: %w", err)
	}

	// Identify the verification key. When the token carries a "kid" header we
	// look it up in the JWKS; otherwise we try each key in turn and accept the
	// first that produces a valid signature.
	v.mu.RLock()
	keySet := v.keySet
	v.mu.RUnlock()

	var p rawClaims
	if err := v.verifyWithKeySet(tok, keySet, &p); err != nil {
		return nil, err
	}

	now := time.Now().Unix()
	if p.Exp > 0 && now > p.Exp {
		return nil, fmt.Errorf("token expired")
	}
	if p.Nbf > 0 && now < p.Nbf-5 {
		return nil, fmt.Errorf("token not yet valid")
	}
	if v.issuerURL != "" && p.Iss != "" {
		if !strings.HasPrefix(p.Iss, v.issuerURL) {
			return nil, fmt.Errorf("issuer mismatch: got %q want prefix %q", p.Iss, v.issuerURL)
		}
	}
	if v.audience != "" {
		if !audContains(p.Aud, v.audience) {
			return nil, fmt.Errorf("audience mismatch: token does not include %q", v.audience)
		}
	}

	subject := p.Sub
	if subject == "" {
		subject = p.Email
	}

	groups := append(p.Groups, p.Roles...)
	groups = append(groups, p.MsGroups...)

	return &Claims{
		Subject: subject,
		Email:   p.Email,
		Name:    p.Name,
		Groups:  groups,
		Issuer:  p.Iss,
	}, nil
}

// rawClaims is the internal payload structure used during verification.
type rawClaims struct {
	Sub   string      `json:"sub"`
	Email string      `json:"email"`
	Name  string      `json:"name"`
	Iss   string      `json:"iss"`
	Aud   interface{} `json:"aud"`
	Exp   int64       `json:"exp"`
	Nbf   int64       `json:"nbf"`

	// Standard group claim names.
	Groups []string `json:"groups"`
	Roles  []string `json:"roles"`
	// Azure AD / Entra ID group membership claim.
	MsGroups []string `json:"group_membership"`
}

// verifyWithKeySet attempts to verify tok's signature using the keys in ks.
// When the token's JOSE header contains a "kid", only the matching key is tried.
// Otherwise every key in the set is tried until one succeeds.
func (v *JWTValidator) verifyWithKeySet(tok *josejwt.JSONWebToken, ks jose.JSONWebKeySet, dest interface{}) error {
	headers := tok.Headers
	if len(headers) == 0 {
		return fmt.Errorf("JWT has no JOSE headers")
	}

	kid := headers[0].KeyID

	if kid != "" {
		candidates := ks.Key(kid)
		if len(candidates) == 0 {
			return fmt.Errorf("no JWKS key with kid %q — JWKS may be stale", kid)
		}
		for _, k := range candidates {
			if err := tok.Claims(k.Public().Key, dest); err == nil {
				return nil
			}
		}
		return fmt.Errorf("JWT signature verification failed for kid %q", kid)
	}

	// No kid: try all keys.
	for _, k := range ks.Keys {
		if err := tok.Claims(k.Public().Key, dest); err == nil {
			return nil
		}
	}
	if len(ks.Keys) == 0 {
		return fmt.Errorf("JWKS contains no keys")
	}
	return fmt.Errorf("JWT signature verification failed against all JWKS keys")
}

// refreshJWKS fetches the current JWKS from the OIDC discovery document.
func (v *JWTValidator) refreshJWKS(ctx context.Context) error {
	wellKnown := v.issuerURL + "/.well-known/openid-configuration"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, wellKnown, nil)
	if err != nil {
		return err
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return fmt.Errorf("OIDC discovery: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("OIDC discovery returned HTTP %d", resp.StatusCode)
	}

	var doc struct{ JWKSURI string `json:"jwks_uri"` }
	if err := json.NewDecoder(resp.Body).Decode(&doc); err != nil {
		return err
	}

	jwksReq, err := http.NewRequestWithContext(ctx, http.MethodGet, doc.JWKSURI, nil)
	if err != nil {
		return err
	}
	jwksResp, err := http.DefaultClient.Do(jwksReq)
	if err != nil {
		return fmt.Errorf("JWKS fetch: %w", err)
	}
	defer jwksResp.Body.Close()

	body, err := io.ReadAll(io.LimitReader(jwksResp.Body, 1<<20))
	if err != nil {
		return err
	}

	var ks jose.JSONWebKeySet
	if err := json.Unmarshal(body, &ks); err != nil {
		return fmt.Errorf("parse JWKS: %w", err)
	}

	v.mu.Lock()
	v.keySet = ks
	v.lastFetch = time.Now()
	v.mu.Unlock()
	return nil
}

func audContains(aud interface{}, want string) bool {
	switch v := aud.(type) {
	case string:
		return v == want
	case []interface{}:
		for _, a := range v {
			if s, ok := a.(string); ok && s == want {
				return true
			}
		}
	}
	return false
}
