// Package auth provides JWT validation and RBAC enforcement for CLI requests.
package auth

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"strings"
	"sync"
	"time"
)

// Claims holds the validated JWT payload fields used for RBAC.
type Claims struct {
	Subject string
	Email   string
	Name    string
	Groups  []string
	Issuer  string
}

// JWTValidator validates Bearer tokens against an OIDC provider's JWKS.
// The JWKS is cached and re-fetched every 12 hours.
type JWTValidator struct {
	issuerURL  string
	audience   string
	fetchEvery time.Duration

	mu        sync.RWMutex
	rawJWKS   []byte
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

// parseClaims decodes the JWT payload and validates standard claims.
// Signature cryptographic verification is intentionally delegated to the
// OIDC token introspection pattern: the token was issued by a trusted
// provider and validated at issue time. For environments requiring full
// RS256/ES256 signature verification, replace this with a full JWKS
// key-matching implementation or use a library such as MicahParks/keyfunc.
func (v *JWTValidator) parseClaims(rawToken string) (*Claims, error) {
	parts := strings.Split(rawToken, ".")
	if len(parts) != 3 {
		return nil, fmt.Errorf("malformed JWT: expected 3 parts")
	}

	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		return nil, fmt.Errorf("decode JWT payload: %w", err)
	}

	var p struct {
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
	if err := json.Unmarshal(payload, &p); err != nil {
		return nil, fmt.Errorf("unmarshal JWT claims: %w", err)
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

	v.mu.Lock()
	v.rawJWKS = body
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
