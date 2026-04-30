// Package platforms contains the platform driver implementations (GitHub, etc.).
package platforms

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"crypto/tls"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"hash"
	"net/http"
	"net/url"
	"path"
	"strings"

	"gateway-service/internal/models"
	"gateway-service/internal/protoutil"
	"gateway-service/internal/secrets"

	pb_models "gateway-service/internal/gen/proto/go/vartrack/v1/models"
	pb_gh "gateway-service/internal/gen/proto/go/vartrack/v1/models/platforms"
)

var _ models.Platform = (*GitHub)(nil)

func init() {
	models.PlatformRegistry.Register("github", newPlatform)
}

// GitHub implements models.Platform for github.com and GitHub Enterprise.
type GitHub struct {
	config *pb_gh.GitHub
	client *http.Client

	token    string
	password string
	secret   string
}

func newPlatform() models.Platform {
	return &GitHub{}
}

func (g *GitHub) Open(ctx context.Context, config *pb_models.Platform, resolver secrets.Resolver, managerName string) (models.Platform, error) {
	ghConfig := config.GetGithub()
	if ghConfig == nil {
		return nil, fmt.Errorf("github driver: configuration is missing or is not a GitHub type")
	}

	g.config = ghConfig

	var err error
	g.token, err = resolver.Resolve(ctx, ghConfig.GetToken(), managerName)
	if err != nil {
		return nil, fmt.Errorf("github: failed to resolve token: %w", err)
	}
	g.password, err = resolver.Resolve(ctx, ghConfig.GetPassword(), managerName)
	if err != nil {
		return nil, fmt.Errorf("github: failed to resolve password: %w", err)
	}
	g.secret, err = resolver.Resolve(ctx, ghConfig.GetSecret(), managerName)
	if err != nil {
		return nil, fmt.Errorf("github: failed to resolve secret: %w", err)
	}

	tlsConfig := &tls.Config{
		InsecureSkipVerify: !ghConfig.GetVerifySsl(), //nolint:gosec
	}

	// Use the proto Duration field; fall back to 30s when not set.
	// Previously this was int32 seconds — Duration makes the unit explicit.
	httpTimeout := protoutil.DurationOrDefault(ghConfig.GetTimeout(), 30e9 /* 30s */)

	g.client = &http.Client{
		Transport: &http.Transport{TLSClientConfig: tlsConfig},
		Timeout:   httpTimeout,
	}

	return g, nil
}

func (g *GitHub) Close(ctx context.Context) error {
	if g.client != nil {
		if tr, ok := g.client.Transport.(*http.Transport); ok {
			tr.CloseIdleConnections()
		}
	}
	return nil
}

func (g *GitHub) EventTypeHeader() string    { return g.config.EventTypeHeader }
func (g *GitHub) SignatureHeader() string    { return g.config.GitScmSignature }
func (g *GitHub) IsPushEvent(et string) bool { return et == g.config.PushEventName }
func (g *GitHub) IsPREvent(et string) bool   { return et == g.config.PrEventName }
func (g *GitHub) Secret() string             { return g.secret }

func (g *GitHub) Auth(ctx context.Context) error {
	reqURL := fmt.Sprintf("%s/user", g.getBaseAPIURL())
	req, err := http.NewRequestWithContext(ctx, "GET", reqURL, nil)
	if err != nil {
		return err
	}

	g.setAuthHeader(req)
	resp, err := g.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("github auth failed: %s", resp.Status)
	}
	return nil
}

func (g *GitHub) WebhookHasher() hash.Hash {
	if g.secret == "" {
		return nil
	}
	return hmac.New(sha256.New, []byte(g.secret))
}

func (g *GitHub) VerifySignature(mac hash.Hash, signatureHeader string) bool {
	if mac == nil {
		return true // No secret configured
	}
	if !strings.HasPrefix(signatureHeader, "sha256=") {
		return false
	}
	expected := hex.EncodeToString(mac.Sum(nil))
	return hmac.Equal([]byte(signatureHeader[7:]), []byte(expected))
}

func (g *GitHub) Repos(ctx context.Context, patterns []string) ([]string, error) {
	apiBase := g.getBaseAPIURL()
	var nextURL string
	if org := g.config.GetOrgName(); org != "" {
		nextURL = fmt.Sprintf("%s/orgs/%s/repos?per_page=100", apiBase, org)
	} else {
		nextURL = fmt.Sprintf("%s/user/repos?per_page=100", apiBase)
	}

	resolvedSet := make(map[string]struct{})

	for nextURL != "" {
		req, err := http.NewRequestWithContext(ctx, "GET", nextURL, nil)
		if err != nil {
			return nil, err
		}

		g.setAuthHeader(req)
		req.Header.Set("Accept", "application/vnd.github+json")

		resp, err := g.client.Do(req)
		if err != nil {
			return nil, err
		}

		if resp.StatusCode != http.StatusOK {
			resp.Body.Close()
			return nil, fmt.Errorf("github API: %s", resp.Status)
		}

		var repos []struct {
			FullName string `json:"full_name"`
		}
		decodeErr := json.NewDecoder(resp.Body).Decode(&repos)
		resp.Body.Close()

		if decodeErr != nil {
			return nil, decodeErr
		}

		for _, repo := range repos {
			for _, pattern := range patterns {
				if matched, _ := path.Match(pattern, repo.FullName); matched {
					resolvedSet[repo.FullName] = struct{}{}
					break
				}
			}
		}
		nextURL = g.getNextPageURL(resp.Header.Get("Link"))
	}

	result := make([]string, 0, len(resolvedSet))
	for repo := range resolvedSet {
		result = append(result, repo)
	}
	return result, nil
}

func (g *GitHub) CreateWebhook(ctx context.Context, repoName, endpoint string) error {
	apiURL := g.getBaseAPIURL()
	targetURL := fmt.Sprintf("%s/%s", strings.TrimSuffix(g.config.Endpoint, "/"), endpoint)

	insecureSSL := "0"
	if !g.config.VerifySsl {
		insecureSSL = "1"
	}

	payload := map[string]interface{}{
		"name":   "web",
		"active": true,
		"events": []string{g.config.PushEventName, g.config.PrEventName},
		"config": map[string]interface{}{
			"url":          targetURL,
			"content_type": "json",
			"secret":       g.secret,
			"insecure_ssl": insecureSSL,
		},
	}

	jsonData, _ := json.Marshal(payload)
	reqURL := fmt.Sprintf("%s/repos/%s/%s/hooks", apiURL, g.config.GetOrgName(), repoName)

	req, err := http.NewRequestWithContext(ctx, "POST", reqURL, bytes.NewBuffer(jsonData))
	if err != nil {
		return err
	}

	g.setAuthHeader(req)
	req.Header.Set("Content-Type", "application/json")

	resp, err := g.client.Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()

	if resp.StatusCode >= 300 {
		return fmt.Errorf("github webhook creation failed: %s", resp.Status)
	}
	return nil
}

func (g *GitHub) setAuthHeader(req *http.Request) {
	if g.token != "" {
		req.Header.Set("Authorization", fmt.Sprintf("Bearer %s", g.token))
	}
}

func (g *GitHub) getBaseAPIURL() string {
	endpoint := strings.TrimSuffix(g.config.Endpoint, "/")
	if endpoint == "" || strings.Contains(endpoint, "github.com") {
		return "https://api.github.com"
	}
	if !strings.Contains(endpoint, "/api/v3") {
		return endpoint + "/api/v3"
	}
	return endpoint
}

func (g *GitHub) getNextPageURL(linkHeader string) string {
	for _, link := range strings.Split(linkHeader, ",") {
		parts := strings.Split(strings.TrimSpace(link), ";")
		if len(parts) > 1 && strings.TrimSpace(parts[1]) == `rel="next"` {
			return strings.Trim(parts[0], "<>")
		}
	}
	return ""
}

func (g *GitHub) ConstructCloneURL(repo string) string {
	fullRepo := repo
	if !strings.Contains(repo, "/") {
		owner := g.config.GetOrgName()
		if owner == "" {
			owner = g.config.GetUsername()
		}
		fullRepo = fmt.Sprintf("%s/%s", owner, repo)
	}
	u, _ := url.Parse(g.config.Endpoint)
	if g.config.Protocol == "ssh" {
		return fmt.Sprintf("git@%s:%s.git", u.Host, fullRepo)
	}
	auth := ""
	if g.token != "" {
		auth = fmt.Sprintf("%s@", g.token)
	}
	return fmt.Sprintf("%s://%s%s/%s.git", u.Scheme, auth, u.Host, fullRepo)
}
