package main

import (
	"context"
	"encoding/json"
	"net/http"
	"strings"
	"time"

	"watcher-service/internal/watcher"
	"vartrack/libs/grants"
)

// checker is what the check endpoint needs of the manager.
type checker interface {
	CheckNow(ctx context.Context, platform, datasource, tenant string) ([]watcher.CheckResult, bool)
}

// checkTimeout is the longest a check asked for now may take: the orchestrator reads the sink, and a heal is asked for.
const checkTimeout = 90 * time.Second

// checkHandler serves POST /admin/check?platform=&datasource=[&tenant=]: look at that rule now and heal it if it drifted,
// instead of at the next poll. It answers with what each watcher of the rule found.
//
// The call acts on the system, so it needs an admin grant signed with the watcher's secret (the same HS256 grants as the
// rest of varTrack's internal calls). Without a secret the watcher runs open, as it does towards the orchestrator, and
// the call is accepted unsigned: that is development only.
func checkHandler(mgr checker, secret string) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		if r.Method != http.MethodPost {
			w.Header().Set("Allow", http.MethodPost)
			fail(w, http.StatusMethodNotAllowed, "POST only")
			return
		}
		if secret != "" {
			token, ok := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer ")
			if !ok || strings.TrimSpace(token) == "" {
				fail(w, http.StatusUnauthorized, "an admin grant is required: Authorization: Bearer <grant>")
				return
			}
			if _, err := grants.Verify(secret, strings.TrimSpace(token), grants.AudienceAdmin, time.Time{}); err != nil {
				fail(w, http.StatusUnauthorized, err.Error())
				return
			}
		}
		platform, datasource := r.URL.Query().Get("platform"), r.URL.Query().Get("datasource")
		if platform == "" || datasource == "" {
			fail(w, http.StatusBadRequest, "platform and datasource are required")
			return
		}
		ctx, cancel := context.WithTimeout(r.Context(), checkTimeout)
		defer cancel()
		results, watched := mgr.CheckNow(ctx, platform, datasource, r.URL.Query().Get("tenant"))
		if !watched {
			fail(w, http.StatusNotFound, platform+"/"+datasource+" is not watched: it has no self-heal, or is suspended")
			return
		}
		_ = json.NewEncoder(w).Encode(map[string]any{"results": results})
	})
}

func fail(w http.ResponseWriter, code int, msg string) {
	w.WriteHeader(code)
	_ = json.NewEncoder(w).Encode(map[string]string{"detail": msg})
}
