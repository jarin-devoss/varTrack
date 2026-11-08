// Package degraded is the list of weaker modes an operator has accepted by name.
//
// A weaker mode (replay protection kept in this process instead of Redis, plain HTTP where TLS was asked
// for) exists only when VARTRACK_ALLOW_DEGRADED names it. It is logged when it is taken, listed by the
// readiness probe for as long as it is on, and some of them are refused in production whatever is named.
package degraded

import (
	"fmt"
	"log/slog"
	"os"
	"sort"
	"strings"
	"sync"
)

// Modes are the names VARTRACK_ALLOW_DEGRADED may hold, with what each one allows.
var Modes = map[string]string{
	"config":            "start without config.cue, on environment variables only",
	"nonce_store":       "keep webhook replay protection in this process instead of Redis",
	"tls":               "serve without TLS when no certificate is available",
	"schema_validation": "write files unvalidated when the cue program is missing",
}

// NeverInProduction are refused in production, whatever is named.
var NeverInProduction = map[string]bool{"nonce_store": true, "tls": true}

var (
	mu     sync.Mutex
	active = map[string]string{}
)

// Allowed reports whether the operator named mode, and it may be used here. It is an error for the
// variable to name a mode that does not exist: a typo must not silently allow nothing.
func Allowed(mode string, production bool) (bool, error) {
	named := map[string]bool{}
	for _, m := range strings.Split(os.Getenv("VARTRACK_ALLOW_DEGRADED"), ",") {
		if m = strings.TrimSpace(m); m != "" {
			named[m] = true
		}
	}
	var unknown []string
	for m := range named {
		if _, ok := Modes[m]; !ok {
			unknown = append(unknown, m)
		}
	}
	if len(unknown) > 0 {
		sort.Strings(unknown)
		return false, fmt.Errorf("VARTRACK_ALLOW_DEGRADED names %s: the modes are %s",
			strings.Join(unknown, ", "), strings.Join(names(), ", "))
	}
	return named[mode] && !(NeverInProduction[mode] && production), nil
}

// On records that mode is in use, and why. Only after Allowed said it may be.
func On(mode, why string) {
	mu.Lock()
	active[mode] = why
	mu.Unlock()
	slog.Warn("gateway: running in a degraded mode the operator allowed", "mode", mode, "why", why)
}

// Active are the modes in use, with the reason each was taken.
func Active() map[string]string {
	mu.Lock()
	defer mu.Unlock()
	out := make(map[string]string, len(active))
	for k, v := range active {
		out[k] = v
	}
	return out
}

// Reset clears the list (tests).
func Reset() {
	mu.Lock()
	active = map[string]string{}
	mu.Unlock()
}

func names() []string {
	out := make([]string, 0, len(Modes))
	for m := range Modes {
		out = append(out, m)
	}
	sort.Strings(out)
	return out
}
