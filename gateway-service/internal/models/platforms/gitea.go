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
	pb_gt "gateway-service/internal/gen/proto/go/vartrack/v1/models/platforms"
)

var _ models.Platform = (*Gitea)(nil)

func init() {
	models.PlatformRegistry.Register("gitea", newGiteaPlatform)
}

// Gitea implements models.Platform for self-hosted Gitea instances.
// The Gitea API is compatible with the GitHub API at /api/v1.
type Gitea struct {
	config *pb_gt.Gitea
	client *http.Client

	token    string
	password string
	secret   string
}

func newGiteaPlatform() models.Platform {
	return &Gitea{}
}

func (g *Gitea) Open(ctx context.Context, config *pb_models.Platform, resolver secrets.Resolver, managerName string) (models.Platform, error) {
	gtConfig := config.GetGitea()
	if gtConfig == nil {
		return nil, fmt.Errorf("gitea driver: configuration is missing or is not a Gitea type")
	}

	g.config = gtConfig

	var err error
	g.token, err = resolver.Resolve(ctx, gtConfig.GetToken(), managerName)
	if err != nil {
		return nil, fmt.Errorf("gitea: failed to resolve token: %w", err)
	}
	g.password, err = resolver.Resolve(ctx, gtConfig.GetPassword(), managerName)
	if err != nil {
		return nil, fmt.Errorf("gitea: failed to resolve password: %w", err)
	}
	g.secret, err = resolver.Resolve(ctx, gtConfig.GetSecret(), managerName)
	if err != nil {
		return nil, fmt.Errorf("gitea: failed to resolve secret: %w", err)
	}

	tlsConfig := &tls.Config{
		InsecureSkipVerify: !gtConfig.GetVerifySsl(), //nolint:gosec
	}

	httpTimeout := protoutil.DurationOrDefault(gtConfig.GetTimeout(), 30e9 /* 30s */)

	g.client = &http.Client{
		Transport: &http.Transport{TLSClientConfig: tlsConfig},
		Timeout:   httpTimeout,
	}

	return g, nil
}

func (g *Gitea) Close(ctx context.Context) error {
	if g.client != nil {
		if tr, ok := g.client.Transport.(*http.Transport); ok {
			tr.CloseIdleConnections()
		}
	}
	return nil
}

func (g *Gitea) EventTypeHeader() string    { return g.config.GetEventTypeHeader() }
func (g *Gitea) SignatureHeader() string    { return g.config.GetGitScmSignature() }
func (g *Gitea) IsPushEvent(et string) bool { return et == g.config.GetPushEventName() }
func (g *Gitea) IsPREvent(et string) bool   { return et == g.config.GetPrEventName() }
func (g *Gitea) Secret() string             { return g.secret }

func (g *Gitea) Auth(ctx context.Context) error {
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
		return fmt.Errorf("gitea auth failed: %s", resp.Status)
	}
	return nil
}

func (g *Gitea) WebhookHasher() hash.Hash {
	if g.secret == "" {
		return nil
	}
	return hmac.New(sha256.New, []byte(g.secret))
}

func (g *Gitea) VerifySignature(mac hash.Hash, signatureHeader string) bool {
	if mac == nil {
		return true
	}
	// Gitea sends the raw hex digest (no "sha256=" prefix).
	expected := hex.EncodeToString(mac.Sum(nil))
	return hmac.Equal([]byte(signatureHeader), []byte(expected))
}

func (g *Gitea) Repos(ctx context.Context, patterns []string) ([]string, error) {
	apiBase := g.getBaseAPIURL()
	pageSize := g.config.GetPageSize()
	if pageSize == 0 {
		pageSize = 50
	}

	var nextURL string
	if org := g.config.GetOrgName(); org != "" {
		nextURL = fmt.Sprintf("%s/orgs/%s/repos?limit=%d&page=1", apiBase, org, pageSize)
	} else {
		nextURL = fmt.Sprintf("%s/repos/search?limit=%d&page=1", apiBase, pageSize)
	}

	resolvedSet := make(map[string]struct{})

	for nextURL != "" {
		req, err := http.NewRequestWithContext(ctx, "GET", nextURL, nil)
		if err != nil {
			return nil, err
		}

		g.setAuthHeader(req)

		resp, err := g.client.Do(req)
		if err != nil {
			return nil, err
		}

		if resp.StatusCode != http.StatusOK {
			resp.Body.Close()
			return nil, fmt.Errorf("gitea API: %s", resp.Status)
		}

		// /repos/search wraps results in {"data": [...]}; /orgs/{org}/repos returns array directly.
		var repos []struct {
			FullName string `json:"full_name"`
		}
		var wrapper struct {
			Data []struct {
				FullName string `json:"full_name"`
			} `json:"data"`
		}

		respBody := json.NewDecoder(resp.Body)
		if strings.Contains(nextURL, "/repos/search") {
			decodeErr := respBody.Decode(&wrapper)
			resp.Body.Close()
			if decodeErr != nil {
				return nil, decodeErr
			}
			for _, r := range wrapper.Data {
				repos = append(repos, struct{ FullName string `json:"full_name"` }{r.FullName})
			}
		} else {
			decodeErr := respBody.Decode(&repos)
			resp.Body.Close()
			if decodeErr != nil {
				return nil, decodeErr
			}
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

func (g *Gitea) CreateWebhook(ctx context.Context, repoName, endpoint string) error {
	apiURL := g.getBaseAPIURL()
	targetURL := fmt.Sprintf("%s/%s", strings.TrimSuffix(g.config.Endpoint, "/"), endpoint)

	payload := map[string]interface{}{
		"active": true,
		"branch_filter": "*",
		"config": map[string]interface{}{
			"url":          targetURL,
			"content_type": "json",
			"secret":       g.secret,
		},
		"events": []string{g.config.GetPushEventName(), g.config.GetPrEventName()},
		"type":   "gitea",
	}

	jsonData, _ := json.Marshal(payload)
	reqURL := fmt.Sprintf("%s/repos/%s/hooks", apiURL, repoName)

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
		return fmt.Errorf("gitea webhook creation failed: %s", resp.Status)
	}
	return nil
}

func (g *Gitea) setAuthHeader(req *http.Request) {
	if g.token != "" {
		req.Header.Set("Authorization", fmt.Sprintf("token %s", g.token))
	} else if g.config.GetUsername() != "" && g.password != "" {
		req.SetBasicAuth(g.config.GetUsername(), g.password)
	}
}

func (g *Gitea) getBaseAPIURL() string {
	return strings.TrimSuffix(g.config.Endpoint, "/") + "/api/v1"
}

func (g *Gitea) getNextPageURL(linkHeader string) string {
	for _, link := range strings.Split(linkHeader, ",") {
		parts := strings.Split(strings.TrimSpace(link), ";")
		if len(parts) > 1 && strings.TrimSpace(parts[1]) == `rel="next"` {
			return strings.Trim(parts[0], "<> ")
		}
	}
	return ""
}

func (g *Gitea) ConstructCloneURL(repo string) string {
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
