// linux_server.go — Local filesystem (linux_server) drift watcher.
//
// VarTrack writes config files to a base directory and stamps a header
// comment on the first line:
//
//	# vartrack managed — env=<env> tenant=<tenant_id>
//
// The watcher walks the base_path, identifies VarTrack-managed files by
// that comment, reads their content, and computes a fingerprint.
//
// On drift (file edited / deleted externally) the watcher triggers the
// orchestrator to re-write the correct content from git.
//
// SSH remote watchers are not supported in this implementation — use a
// shared network filesystem (NFS/SSHFS) mounted locally instead.
//
// rule_config keys consumed:
//
//	linux_server_base_path  string — root directory to watch (required)
//	linux_server_extensions string — comma-separated extensions to include
//	                                  (default: ".env,.json,.yaml,.properties,.ini")
package watcher

import (
	"bufio"
	"context"
	"fmt"
	"io"
	"log/slog"
	"os"
	"path/filepath"
	"strings"

	"watcher-service/internal/config"
	dsv1 "watcher-service/internal/gen/proto/go/vartrack/v1/models/datasources"
	models "watcher-service/internal/gen/proto/go/vartrack/v1/models"
	"watcher-service/internal/healer"
)

// LinuxServerWatcher watches a local directory tree for drift.
type LinuxServerWatcher struct {
	name       string
	basePath   string
	extensions map[string]bool
	healer     *healer.Healer
	healOpts   healer.HealRequest
}

// NewLinuxServerWatcher creates a LinuxServerWatcher from rule_config.
func NewLinuxServerWatcher(
	_ context.Context,
	dsCfg *dsv1.LinuxServerConfig,
	rule *models.Rule,
	h *healer.Healer,
) (*LinuxServerWatcher, error) {
	// Base path: use base_directory from proto, fall back to first file_path dir.
	basePath := dsCfg.GetBaseDirectory()
	if basePath == "" && len(dsCfg.GetFilePaths()) > 0 {
		basePath = dsCfg.GetFilePaths()[0]
	}
	if basePath == "" {
		return nil, fmt.Errorf("linux_server watcher %s: base_directory is required", config.RuleName(rule))
	}

	if _, err := os.Stat(basePath); err != nil {
		return nil, fmt.Errorf("linux_server watcher %s: base_directory %q: %w", config.RuleName(rule), basePath, err)
	}

	// Default extensions for VarTrack-generated config files.
	extStr := ".env,.json,.yaml,.yml,.properties,.ini"
	extensions := map[string]bool{}
	for _, e := range strings.Split(extStr, ",") {
		e = strings.TrimSpace(e)
		if e != "" {
			extensions[e] = true
		}
	}

	slog.Info("linux_server watcher: configured",
		"watcher", config.RuleName(rule), "base_path", basePath)

	return &LinuxServerWatcher{
		name:       config.RuleName(rule),
		basePath:   basePath,
		extensions: extensions,
		healer:     h,
		healOpts: healer.HealRequest{
			Datasource: rule.GetDatasource(),
			Platform:   rule.GetPlatform(),
		},
	}, nil
}

// Name implements Watcher.
func (w *LinuxServerWatcher) Name() string { return "linux_server/" + w.name }

// Snapshot walks basePath, reads every VarTrack-managed file, and returns
// a fingerprint of their contents.
//
// A file is considered VarTrack-managed if its first non-empty line starts
// with "# vartrack managed".
func (w *LinuxServerWatcher) Snapshot(ctx context.Context) (string, error) {
	records := make(map[string]string)

	err := filepath.WalkDir(w.basePath, func(path string, d os.DirEntry, err error) error {
		if err != nil {
			return nil // skip unreadable entries
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		default:
		}

		if d.IsDir() {
			return nil
		}

		ext := strings.ToLower(filepath.Ext(path))
		if !w.extensions[ext] {
			return nil
		}

		content, managed, readErr := readManagedFile(path)
		if readErr != nil || !managed {
			return nil
		}

		relPath, _ := filepath.Rel(w.basePath, path)
		records[relPath] = content
		return nil
	})
	if err != nil {
		return "", fmt.Errorf("linux_server snapshot %s: walk %s: %w", w.name, w.basePath, err)
	}

	return FingerprintRecords(records), nil
}

// Restore implements Watcher.
func (w *LinuxServerWatcher) Restore(ctx context.Context) error {
	slog.Info("linux_server watcher: triggering heal", "watcher", w.name)
	return w.healer.Heal(ctx, w.healOpts)
}

// Close is a no-op.
func (w *LinuxServerWatcher) Close() error { return nil }

// ─── helpers ──────────────────────────────────────────────────────────────────

// readManagedFile reads a file and reports whether its first non-empty line
// starts with "# vartrack managed".  Returns the full content and true if
// managed, or "", false if not managed or unreadable.
func readManagedFile(path string) (content string, managed bool, err error) {
	f, err := os.Open(path)
	if err != nil {
		return "", false, err
	}
	defer f.Close()

	// Limit reads to 1 MiB to avoid fingerprinting huge files.
	limited := io.LimitReader(f, 1<<20)

	scanner := bufio.NewScanner(limited)

	// Check first non-empty line for VarTrack header.
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" {
			continue
		}
		if !strings.HasPrefix(line, "# vartrack managed") {
			return "", false, nil // not managed
		}
		break
	}
	if err := scanner.Err(); err != nil {
		return "", false, err
	}

	// Re-read full content for fingerprinting.
	if _, err := f.Seek(0, io.SeekStart); err != nil {
		return "", false, err
	}
	data, err := io.ReadAll(io.LimitReader(f, 1<<20))
	if err != nil {
		return "", false, err
	}
	return string(data), true, nil
}
