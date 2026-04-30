// configmap.go — Kubernetes ConfigMap drift watcher.
//
// VarTrack stamps every ConfigMap it writes with:
//
//	metadata.labels["app.kubernetes.io/managed-by"] = "vartrack"
//	metadata.labels["vartrack.io/tenant"]           = "<tenant>"
//	metadata.labels["vartrack.io/env"]              = "<env>"
//	...
//
// The watcher lists all ConfigMaps matching that label selector across the
// configured namespace(s) and fingerprints their .data maps so that any
// external edit (kubectl edit / helm override / direct API patch) is detected.
//
// rule_config keys consumed:
//
//	configmap_namespace   string — namespace to watch (default: "default")
//	configmap_kubeconfig  string — path to kubeconfig (default: in-cluster)
//	configmap_context     string — kube context to use
package watcher

import (
	"context"
	"fmt"
	"log/slog"

	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/client-go/kubernetes"
	"k8s.io/client-go/rest"
	"k8s.io/client-go/tools/clientcmd"

	"watcher-service/internal/config"
	dsv1 "watcher-service/internal/gen/proto/go/vartrack/v1/models/datasources"
	models "watcher-service/internal/gen/proto/go/vartrack/v1/models"
	"watcher-service/internal/healer"
)

// labelSelector matches all ConfigMaps managed by VarTrack.
const vtLabelSelector = "app.kubernetes.io/managed-by=vartrack"

// ConfigMapWatcher watches Kubernetes ConfigMaps for drift.
type ConfigMapWatcher struct {
	name      string
	client    kubernetes.Interface
	namespace string
	healer    *healer.Healer
	healOpts  healer.HealRequest
}

// NewConfigMapWatcher builds a Kubernetes client from rule_config and returns
// a ready ConfigMapWatcher.
func NewConfigMapWatcher(
	ctx context.Context,
	dsCfg *dsv1.ConfigMapConfig,
	rule *models.Rule,
	h *healer.Healer,
) (*ConfigMapWatcher, error) {
	namespace := dsCfg.GetNamespace()
	if namespace == "" {
		namespace = "default"
	}
	kubeconfigPath := dsCfg.GetKubeconfigPath()
	kubeContext := dsCfg.GetContext()

	cfg, err := buildKubeConfig(kubeconfigPath, kubeContext)
	if err != nil {
		return nil, fmt.Errorf("configmap watcher %s: build kube config: %w", config.RuleName(rule), err)
	}

	client, err := kubernetes.NewForConfig(cfg)
	if err != nil {
		return nil, fmt.Errorf("configmap watcher %s: build kube client: %w", config.RuleName(rule), err)
	}

	// Verify connectivity.
	if _, err := client.CoreV1().Namespaces().Get(ctx, namespace, metav1.GetOptions{}); err != nil {
		return nil, fmt.Errorf("configmap watcher %s: connect to cluster: %w", config.RuleName(rule), err)
	}

	slog.Info("configmap watcher: connected",
		"watcher", config.RuleName(rule), "namespace", namespace)

	return &ConfigMapWatcher{
		name:      config.RuleName(rule),
		client:    client,
		namespace: namespace,
		healer:    h,
		healOpts: healer.HealRequest{
			Datasource: rule.GetDatasource(),
			Platform:   rule.GetPlatform(),
		},
	}, nil
}

// Name implements Watcher.
func (w *ConfigMapWatcher) Name() string { return "configmap/" + w.name }

// Snapshot lists all VarTrack-managed ConfigMaps in the namespace and
// returns a fingerprint of their data fields.
func (w *ConfigMapWatcher) Snapshot(ctx context.Context) (string, error) {
	list, err := w.client.CoreV1().ConfigMaps(w.namespace).List(ctx, metav1.ListOptions{
		LabelSelector: vtLabelSelector,
	})
	if err != nil {
		return "", fmt.Errorf("configmap snapshot %s: list: %w", w.name, err)
	}

	records := make(map[string]string)
	for _, cm := range list.Items {
		for k, v := range cm.Data {
			// Qualify key with the ConfigMap name to avoid cross-CM collisions.
			records[cm.Name+"/"+k] = v
		}
		// Also include binary data keys (binaryData) by key name only — we
		// fingerprint the key's existence but not the raw bytes to avoid
		// base64 bloat.
		for k := range cm.BinaryData {
			records[cm.Name+"/binary/"+k] = "present"
		}
	}

	return FingerprintRecords(records), nil
}

// Restore implements Watcher.
func (w *ConfigMapWatcher) Restore(ctx context.Context) error {
	slog.Info("configmap watcher: triggering heal", "watcher", w.name)
	return w.healer.Heal(ctx, w.healOpts)
}

// Close is a no-op (k8s client uses a shared HTTP transport).
func (w *ConfigMapWatcher) Close() error { return nil }

// ─── kubeconfig helper ────────────────────────────────────────────────────────

func buildKubeConfig(kubeconfigPath, context string) (*rest.Config, error) {
	// 1. In-cluster config (watcher running as a Pod).
	if kubeconfigPath == "" {
		if cfg, err := rest.InClusterConfig(); err == nil {
			return cfg, nil
		}
	}

	// 2. kubeconfig file (watcher running outside the cluster).
	rules := clientcmd.NewDefaultClientConfigLoadingRules()
	if kubeconfigPath != "" {
		rules.ExplicitPath = kubeconfigPath
	}

	overrides := &clientcmd.ConfigOverrides{}
	if context != "" {
		overrides.CurrentContext = context
	}

	return clientcmd.NewNonInteractiveDeferredLoadingClientConfig(
		rules, overrides,
	).ClientConfig()
}
