// helm.go — Helm release drift watcher.
//
// VarTrack manages Helm releases by running `helm upgrade --install` with
// values derived from git config files.  It stamps every managed release
// with annotations on the underlying Kubernetes resources:
//
//	app.kubernetes.io/managed-by: vartrack
//	vartrack.io/tenant:           <tenant>
//	...
//
// Drift detection strategy:
//
// Helm v3 stores release state as Kubernetes Secrets labelled:
//
//	owner=helm, name=<release>, status=deployed
//
// Each Secret's "release" field contains base64(gzip(JSON)).  The watcher
// decodes the JSON, extracts the user-supplied values (`config` field),
// flattens them into key=value pairs, and fingerprints the result.
// Any direct `helm upgrade` by an external actor changes the values hash.
package watcher

import (
	"bytes"
	"compress/gzip"
	"context"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"io"
	"log/slog"
	"sort"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"

	"watcher-service/internal/config"
	dsv1 "watcher-service/internal/gen/proto/go/vartrack/v1/models/datasources"
	models "watcher-service/internal/gen/proto/go/vartrack/v1/models"
	"watcher-service/internal/healer"
)

// HelmWatcher detects drift in Helm-managed releases.
type HelmWatcher struct {
	name        string
	client      kubernetes.Interface
	namespace   string
	releaseName string
	healer      *healer.Healer
	healOpts    healer.HealRequest
}

// NewHelmWatcher builds a Kubernetes client and returns a ready HelmWatcher.
func NewHelmWatcher(
	ctx context.Context,
	dsCfg *dsv1.HelmValuesConfig,
	rule *models.Rule,
	h *healer.Healer,
) (*HelmWatcher, error) {
	namespace := dsCfg.GetNamespace()
	if namespace == "" {
		namespace = "default"
	}

	kubeCfg, err := buildKubeConfig(dsCfg.GetKubeconfigPath(), dsCfg.GetContext())
	if err != nil {
		return nil, fmt.Errorf("helm watcher %s: build kube config: %w", config.RuleName(rule), err)
	}

	client, err := kubernetes.NewForConfig(kubeCfg)
	if err != nil {
		return nil, fmt.Errorf("helm watcher %s: build kube client: %w", config.RuleName(rule), err)
	}

	if _, err := client.CoreV1().Namespaces().Get(ctx, namespace, metav1.GetOptions{}); err != nil {
		return nil, fmt.Errorf("helm watcher %s: connect to cluster: %w", config.RuleName(rule), err)
	}

	slog.Info("helm watcher: connected",
		"watcher", config.RuleName(rule),
		"namespace", namespace,
		"release", dsCfg.GetReleaseName(),
	)

	return &HelmWatcher{
		name:        config.RuleName(rule),
		client:      client,
		namespace:   namespace,
		releaseName: dsCfg.GetReleaseName(),
		healer:      h,
		healOpts: healer.HealRequest{
			Datasource: rule.GetDatasource(),
			Platform:   rule.GetPlatform(),
		},
	}, nil
}

// Name implements Watcher.
func (w *HelmWatcher) Name() string { return "helm/" + w.name }

// Snapshot reads the latest deployed Helm release Secret and fingerprints
// the user-supplied values (the `config` field in the release JSON).
//
// If the release is uninstalled (no Secret) the fingerprint is the empty hash.
// Any change to values — from a direct `helm upgrade` — alters the hash.
func (w *HelmWatcher) Snapshot(ctx context.Context) (string, error) {
	labelSel := fmt.Sprintf("owner=helm,name=%s,status=deployed", w.releaseName)
	secrets, err := w.client.CoreV1().Secrets(w.namespace).List(ctx, metav1.ListOptions{
		LabelSelector: labelSel,
	})
	if err != nil {
		return "", fmt.Errorf("helm snapshot %s: list secrets: %w", w.name, err)
	}

	if len(secrets.Items) == 0 {
		slog.Debug("helm watcher: no deployed release found", "release", w.releaseName)
		return FingerprintRecords(nil), nil
	}

	// Sort by name descending — "sh.helm.release.v1.name.v10" > "...v9".
	sort.Slice(secrets.Items, func(i, j int) bool {
		return secrets.Items[i].Name > secrets.Items[j].Name
	})
	releaseData := secrets.Items[0].Data["release"]

	values, err := decodeHelmValues(releaseData)
	if err != nil {
		slog.Warn("helm watcher: decode release failed, using empty fingerprint",
			"watcher", w.name, "error", err)
		return FingerprintRecords(nil), nil
	}

	records := make(map[string]string)
	flattenValues("", values, records)
	return FingerprintRecords(records), nil
}

// Restore implements Watcher.
func (w *HelmWatcher) Restore(ctx context.Context) error {
	slog.Info("helm watcher: triggering heal", "watcher", w.name)
	return w.healer.Heal(ctx, w.healOpts)
}

// Close is a no-op (k8s HTTP transport is shared).
func (w *HelmWatcher) Close() error { return nil }

// ─── Helm release decoding ────────────────────────────────────────────────────

type helmRelease struct {
	Config map[string]any `json:"config"` // user-supplied values
}

// decodeHelmValues extracts the user-supplied Helm values from a release Secret.
// Encoding: base64(gzip(JSON)) — Helm v3 double-base64 encoding.
func decodeHelmValues(raw []byte) (map[string]any, error) {
	// Helm v3: the secret value is itself base64-encoded (on top of k8s base64).
	b64, err := base64.StdEncoding.DecodeString(string(raw))
	if err != nil {
		b64 = raw // fall through with raw bytes
	}

	// Gunzip if magic bytes present.
	jsonBytes := b64
	if len(b64) >= 2 && b64[0] == 0x1f && b64[1] == 0x8b {
		gr, err := gzip.NewReader(bytes.NewReader(b64))
		if err != nil {
			return nil, fmt.Errorf("gzip reader: %w", err)
		}
		defer gr.Close()
		jsonBytes, err = io.ReadAll(gr)
		if err != nil {
			return nil, fmt.Errorf("gzip read: %w", err)
		}
	}

	var rel helmRelease
	if err := json.Unmarshal(jsonBytes, &rel); err != nil {
		return nil, fmt.Errorf("unmarshal release JSON: %w", err)
	}
	if rel.Config == nil {
		return map[string]any{}, nil
	}
	return rel.Config, nil
}

// flattenValues recursively flattens a nested map into dot-separated keys.
func flattenValues(prefix string, v any, out map[string]string) {
	switch val := v.(type) {
	case map[string]any:
		for k, child := range val {
			key := k
			if prefix != "" {
				key = prefix + "." + k
			}
			flattenValues(key, child, out)
		}
	case []any:
		for i, child := range val {
			flattenValues(fmt.Sprintf("%s[%d]", prefix, i), child, out)
		}
	default:
		out[prefix] = fmt.Sprintf("%v", v)
	}
}
