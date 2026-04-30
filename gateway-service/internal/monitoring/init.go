package monitoring

import (
	"context"
	"runtime"
	"time"
)

// BuildInfo carries static build-time data injected via -ldflags.
// Zero values produce placeholder labels that are still valid for scraping.
type BuildInfo struct {
	Version   string
	Commit    string
	GoVersion string
}

// DefaultBuildInfo returns build info with sensible defaults for the
// current binary. GoVersion is always available at runtime.
func DefaultBuildInfo() BuildInfo {
	return BuildInfo{
		Version:   "dev",
		Commit:    "unknown",
		GoVersion: runtime.Version(),
	}
}

// Init performs one-time setup for the monitoring subsystem:
//  1. Emits build info gauge.
//  2. Starts the gRPC connection state poller.
//
// Call this once from main(), after the gRPC connection is established.
func Init(ctx context.Context, info BuildInfo, conn ConnStateChecker) *GatewayMetrics {
	m := DefaultMetrics()
	m.SetBuildInfo(info.Version, info.Commit, info.GoVersion)

	if conn != nil {
		StartConnStatePoller(ctx, conn, m, 15*time.Second)
	}

	return m
}
