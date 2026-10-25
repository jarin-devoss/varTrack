package platforms

// caller.go — the Git API as the person holding a token sees it.
//
// The CLI API authorizes by Git permission: who a caller is, and what they
// may do in a rule's repositories, is asked of the Git platform with the
// caller's own token. GitHub and Gitea answer these three alike:
//
//	GET /user                  → {"login": …}
//	GET /repos/{owner}/{repo}  → {"permissions": {"admin", "push", "pull"}}
//	GET /user/repos            → [{"full_name", "permissions"}, …] (paged)
//	GET /repos/{o}/{r}/branches/{b} → {"protected", "user_can_push" (Gitea)}

import (
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"net/url"
	"strings"

	"gateway-service/internal/models"
)

// callerAPI asks a platform's REST API with a caller's token.
type callerAPI struct {
	client    *http.Client
	base      string // the API root, e.g. "http://gitea:3000/api/v1"
	scheme    string // Authorization scheme: "token" (Gitea) or "Bearer" (GitHub)
	pageParam string // page size parameter: "limit" (Gitea) or "per_page" (GitHub)
}

type repoPermissions struct {
	Admin bool `json:"admin"`
	Push  bool `json:"push"`
	Pull  bool `json:"pull"`
}

func (p repoPermissions) permission() models.RepoPermission {
	return models.RepoPermission{Read: p.Pull || p.Push || p.Admin, Write: p.Push || p.Admin, Admin: p.Admin}
}

// get fetches reqURL with token into out. It returns the response's Link
// header; ok is false for 404 (no such repository, or one hidden from the
// caller).
func (c callerAPI) get(ctx context.Context, token, reqURL string, out any) (link string, ok bool, err error) {
	if strings.TrimSpace(token) == "" {
		return "", false, models.ErrTokenRejected
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, reqURL, nil)
	if err != nil {
		return "", false, err
	}
	req.Header.Set("Authorization", c.scheme+" "+token)
	req.Header.Set("Accept", "application/json")
	client := c.client
	if client == nil {
		client = http.DefaultClient
	}
	resp, err := client.Do(req)
	if err != nil {
		return "", false, err
	}
	defer resp.Body.Close()
	switch {
	case resp.StatusCode == http.StatusUnauthorized:
		return "", false, models.ErrTokenRejected
	case resp.StatusCode == http.StatusNotFound:
		return "", false, nil
	case resp.StatusCode != http.StatusOK:
		body, _ := io.ReadAll(io.LimitReader(resp.Body, 256))
		return "", false, fmt.Errorf("%s: %s %s", reqURL, resp.Status, strings.TrimSpace(string(body)))
	}
	if err := json.NewDecoder(resp.Body).Decode(out); err != nil {
		return "", false, fmt.Errorf("%s: %w", reqURL, err)
	}
	return resp.Header.Get("Link"), true, nil
}

// Login is the caller's login on the platform.
func (c callerAPI) Login(ctx context.Context, token string) (string, error) {
	var user struct {
		Login string `json:"login"`
	}
	if _, ok, err := c.get(ctx, token, c.base+"/user", &user); err != nil {
		return "", err
	} else if !ok || user.Login == "" {
		return "", models.ErrTokenRejected
	}
	return user.Login, nil
}

// Permission is the caller's permission in repo ("owner/name"); none when
// the repository does not exist or is hidden from them.
func (c callerAPI) Permission(ctx context.Context, token, repo string) (models.RepoPermission, error) {
	owner, name, found := strings.Cut(strings.Trim(repo, "/"), "/")
	if !found || owner == "" || name == "" {
		return models.RepoPermission{}, fmt.Errorf("repository %q is not owner/name", repo)
	}
	var body struct {
		Permissions *repoPermissions `json:"permissions"`
	}
	_, ok, err := c.get(ctx, token, fmt.Sprintf("%s/repos/%s/%s", c.base, url.PathEscape(owner), url.PathEscape(name)), &body)
	if err != nil || !ok {
		return models.RepoPermission{}, err
	}
	if body.Permissions == nil {
		// Visible without a permissions block: a public repository the
		// caller is not a member of.
		return models.RepoPermission{Read: true}, nil
	}
	return body.Permissions.permission(), nil
}

// CanPush reports whether the caller may push to branch of repo directly.
// Gitea answers it (user_can_push, branch protection included); GitHub only
// says whether the branch is protected, and then only an admin may push.
func (c callerAPI) CanPush(ctx context.Context, token, repo, branch string) (bool, error) {
	owner, name, found := strings.Cut(strings.Trim(repo, "/"), "/")
	if !found || owner == "" || name == "" {
		return false, fmt.Errorf("repository %q is not owner/name", repo)
	}
	var b struct {
		Protected   bool  `json:"protected"`
		UserCanPush *bool `json:"user_can_push"`
	}
	_, ok, err := c.get(ctx, token, fmt.Sprintf("%s/repos/%s/%s/branches/%s",
		c.base, url.PathEscape(owner), url.PathEscape(name), url.PathEscape(branch)), &b)
	if err != nil {
		return false, err
	}
	if ok && b.UserCanPush != nil {
		return *b.UserCanPush, nil
	}
	perm, err := c.Permission(ctx, token, repo)
	if err != nil {
		return false, err
	}
	if ok && b.Protected {
		return perm.Admin, nil
	}
	return perm.Write, nil // a missing branch: a push creates it
}

// Repos is every repository the caller can see, with their permission.
func (c callerAPI) Repos(ctx context.Context, token string) (map[string]models.RepoPermission, error) {
	out := map[string]models.RepoPermission{}
	next := fmt.Sprintf("%s/user/repos?%s=50&page=1", c.base, c.pageParam)
	for pages := 0; next != "" && pages < 100; pages++ {
		var repos []struct {
			FullName    string           `json:"full_name"`
			Permissions *repoPermissions `json:"permissions"`
		}
		link, ok, err := c.get(ctx, token, next, &repos)
		if err != nil {
			return nil, err
		}
		if !ok {
			break
		}
		for _, r := range repos {
			perm := models.RepoPermission{Read: true}
			if r.Permissions != nil {
				perm = r.Permissions.permission()
			}
			out[r.FullName] = perm
		}
		next = nextLink(link)
	}
	return out, nil
}

// nextLink is the rel="next" URL of a Link header, or "".
func nextLink(header string) string {
	for _, part := range strings.Split(header, ",") {
		fields := strings.Split(strings.TrimSpace(part), ";")
		if len(fields) > 1 && strings.TrimSpace(fields[1]) == `rel="next"` {
			return strings.Trim(fields[0], "<> ")
		}
	}
	return ""
}
