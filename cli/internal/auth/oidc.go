// Package auth handles OIDC browser-based login and static token management.
package auth

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"html"
	"net"
	"net/http"
	"net/url"
	"os/exec"
	"runtime"
	"strings"
	"time"
)

// TokenSet holds the tokens returned after a successful OIDC login.
type TokenSet struct {
	AccessToken  string
	RefreshToken string
	IDToken      string
	ExpiresAt    time.Time
}

// OIDCConfig holds the OIDC provider configuration for the login flow.
type OIDCConfig struct {
	IssuerURL    string
	ClientID     string
	ClientSecret string // optional for PKCE-only flows
	Scopes       []string
	CallbackPort int // local port for the redirect callback (default 8085)
}

// DiscoveryDoc is the minimal subset of the OIDC discovery document.
type DiscoveryDoc struct {
	AuthorizationEndpoint string `json:"authorization_endpoint"`
	TokenEndpoint         string `json:"token_endpoint"`
}

// Login performs the OIDC Authorization Code + PKCE flow.
// It opens the system browser, starts a temporary local HTTP server to
// receive the callback, and exchanges the code for tokens.
func Login(ctx context.Context, cfg OIDCConfig) (*TokenSet, error) {
	if len(cfg.Scopes) == 0 {
		cfg.Scopes = []string{"openid", "email", "profile", "offline_access"}
	}
	if cfg.CallbackPort == 0 {
		cfg.CallbackPort = 8085
	}

	// Discover endpoints.
	doc, err := discover(ctx, cfg.IssuerURL)
	if err != nil {
		return nil, fmt.Errorf("oidc discover: %w", err)
	}

	// Generate PKCE verifier + challenge.
	verifier, challenge, err := pkce()
	if err != nil {
		return nil, fmt.Errorf("pkce: %w", err)
	}

	// Generate state nonce.
	state, err := randomBase64(16)
	if err != nil {
		return nil, fmt.Errorf("state: %w", err)
	}

	redirectURI := fmt.Sprintf("http://localhost:%d/callback", cfg.CallbackPort)

	// Build authorization URL.
	authURL := buildAuthURL(doc.AuthorizationEndpoint, cfg, redirectURI, state, challenge)

	// Start local callback server.
	codeCh := make(chan string, 1)
	errCh  := make(chan error, 1)

	srv := &http.Server{Addr: fmt.Sprintf(":%d", cfg.CallbackPort)}
	http.HandleFunc("/callback", func(w http.ResponseWriter, r *http.Request) {
		q := r.URL.Query()
		if q.Get("state") != state {
			errCh <- fmt.Errorf("state mismatch")
			http.Error(w, "state mismatch", http.StatusBadRequest)
			return
		}
		if errParam := q.Get("error"); errParam != "" {
			errCh <- fmt.Errorf("auth error: %s — %s", errParam, q.Get("error_description"))
			fmt.Fprintf(w, "<html><body><h2>Login failed: %s</h2></body></html>", html.EscapeString(errParam))
			return
		}
		codeCh <- q.Get("code")
		fmt.Fprint(w, "<html><body><h2>Login successful — you may close this tab.</h2></body></html>")
	})

	ln, err := net.Listen("tcp", srv.Addr)
	if err != nil {
		return nil, fmt.Errorf("start callback server on %s: %w", srv.Addr, err)
	}
	go func() {
		if serveErr := srv.Serve(ln); serveErr != nil && serveErr != http.ErrServerClosed {
			errCh <- serveErr
		}
	}()
	defer srv.Shutdown(context.Background())

	// Open browser.
	fmt.Printf("Opening browser for login...\nIf the browser does not open, visit:\n  %s\n\n", authURL)
	openBrowser(authURL)

	// Wait for code or error.
	var code string
	select {
	case code = <-codeCh:
	case err = <-errCh:
		return nil, err
	case <-ctx.Done():
		return nil, ctx.Err()
	case <-time.After(5 * time.Minute):
		return nil, fmt.Errorf("login timed out after 5 minutes")
	}

	// Exchange code for tokens.
	tokens, err := exchangeCode(ctx, doc.TokenEndpoint, cfg, redirectURI, code, verifier)
	if err != nil {
		return nil, fmt.Errorf("token exchange: %w", err)
	}
	return tokens, nil
}

// ─── internal helpers ────────────────────────────────────────────────────────

func discover(ctx context.Context, issuerURL string) (*DiscoveryDoc, error) {
	wellKnown := strings.TrimRight(issuerURL, "/") + "/.well-known/openid-configuration"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, wellKnown, nil)
	if err != nil {
		return nil, err
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return nil, fmt.Errorf("discovery returned HTTP %d", resp.StatusCode)
	}
	var doc DiscoveryDoc
	if err := json.NewDecoder(resp.Body).Decode(&doc); err != nil {
		return nil, err
	}
	return &doc, nil
}

func pkce() (verifier, challenge string, err error) {
	b := make([]byte, 32)
	if _, err = rand.Read(b); err != nil {
		return
	}
	verifier = base64.RawURLEncoding.EncodeToString(b)
	h := sha256.Sum256([]byte(verifier))
	challenge = base64.RawURLEncoding.EncodeToString(h[:])
	return
}

func randomBase64(n int) (string, error) {
	b := make([]byte, n)
	if _, err := rand.Read(b); err != nil {
		return "", err
	}
	return base64.RawURLEncoding.EncodeToString(b), nil
}

func buildAuthURL(endpoint string, cfg OIDCConfig, redirectURI, state, challenge string) string {
	v := url.Values{
		"response_type":         {"code"},
		"client_id":             {cfg.ClientID},
		"redirect_uri":          {redirectURI},
		"scope":                 {strings.Join(cfg.Scopes, " ")},
		"state":                 {state},
		"code_challenge":        {challenge},
		"code_challenge_method": {"S256"},
	}
	return endpoint + "?" + v.Encode()
}

func exchangeCode(
	ctx context.Context,
	tokenEndpoint string,
	cfg OIDCConfig,
	redirectURI, code, verifier string,
) (*TokenSet, error) {
	v := url.Values{
		"grant_type":    {"authorization_code"},
		"client_id":     {cfg.ClientID},
		"redirect_uri":  {redirectURI},
		"code":          {code},
		"code_verifier": {verifier},
	}
	if cfg.ClientSecret != "" {
		v.Set("client_secret", cfg.ClientSecret)
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, tokenEndpoint, strings.NewReader(v.Encode()))
	if err != nil {
		return nil, err
	}
	req.Header.Set("Content-Type", "application/x-www-form-urlencoded")

	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, err
	}
	defer resp.Body.Close()

	var tok struct {
		AccessToken  string `json:"access_token"`
		RefreshToken string `json:"refresh_token"`
		IDToken      string `json:"id_token"`
		ExpiresIn    int    `json:"expires_in"`
		Error        string `json:"error"`
		ErrorDesc    string `json:"error_description"`
	}
	if err := json.NewDecoder(resp.Body).Decode(&tok); err != nil {
		return nil, err
	}
	if tok.Error != "" {
		return nil, fmt.Errorf("%s: %s", tok.Error, tok.ErrorDesc)
	}

	expiresAt := time.Now().Add(time.Duration(tok.ExpiresIn) * time.Second)
	return &TokenSet{
		AccessToken:  tok.AccessToken,
		RefreshToken: tok.RefreshToken,
		IDToken:      tok.IDToken,
		ExpiresAt:    expiresAt,
	}, nil
}

func openBrowser(rawURL string) {
	var cmd string
	var args []string
	switch runtime.GOOS {
	case "darwin":
		cmd, args = "open", []string{rawURL}
	case "windows":
		cmd, args = "cmd", []string{"/c", "start", rawURL}
	default:
		cmd, args = "xdg-open", []string{rawURL}
	}
	_ = exec.Command(cmd, args...).Start()
}
