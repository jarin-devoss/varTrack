// Package secretmanagers contains secret manager driver implementations.
package secretmanagers

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"errors"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"sync"
	"time"

	"gateway-service/internal/models"
	"gateway-service/internal/protoutil"

	pb_enums "gateway-service/internal/gen/proto/go/vartrack/v1/enums"
	pb_models "gateway-service/internal/gen/proto/go/vartrack/v1/models"
	pb_vault "gateway-service/internal/gen/proto/go/vartrack/v1/models/secret_managers"

	vault "github.com/hashicorp/vault/api"
	"github.com/hashicorp/vault/api/auth/approle"
	authk8s "github.com/hashicorp/vault/api/auth/kubernetes"
	authuserpass "github.com/hashicorp/vault/api/auth/userpass"
)

var _ models.SecretManager = (*Vault)(nil)

func init() {
	models.SecretManagerRegistry.Register("vault", newVault)
}

// Vault implements models.SecretManager backed by HashiCorp Vault.
type Vault struct {
	config *pb_vault.VaultConfig
	client *vault.Client

	mu          sync.RWMutex
	renewStopCh chan struct{}
	watcherDone chan struct{}
}

func newVault() models.SecretManager {
	return &Vault{}
}

// Open creates and authenticates a Vault client from the given config.
func (v *Vault) Open(ctx context.Context, config *pb_models.SecretManager) (models.SecretManager, error) {
	vaultConfig := config.GetVault()
	if vaultConfig == nil {
		return nil, fmt.Errorf("vault driver: configuration is missing or not a Vault type")
	}

	v.config = vaultConfig
	v.renewStopCh = make(chan struct{})
	v.watcherDone = make(chan struct{})

	client, secret, err := v.buildClientWithSecret(ctx)
	if err != nil {
		return nil, fmt.Errorf("vault driver: %w", err)
	}

	v.client = client

	if secret != nil && secret.Auth != nil {
		go v.watchTokenLifetime(secret)
	} else {
		go v.pollStaticToken()
	}

	return v, nil
}

// Close stops background goroutines and clears the token. Idempotent.
func (v *Vault) Close(_ context.Context) error {
	select {
	case <-v.renewStopCh:
	default:
		close(v.renewStopCh)
	}

	select {
	case <-v.watcherDone:
	case <-time.After(5 * time.Second):
		slog.Warn("vault: token watcher did not exit within 5s")
	}

	v.mu.Lock()
	defer v.mu.Unlock()
	if v.client != nil {
		v.client.ClearToken()
	}
	return nil
}

// Ping verifies Vault is reachable, unsealed, and the token is valid.
func (v *Vault) Ping(ctx context.Context) error {
	v.mu.RLock()
	client := v.client
	v.mu.RUnlock()

	if client == nil {
		return fmt.Errorf("vault client not initialized")
	}

	health, err := client.Sys().HealthWithContext(ctx)
	if err != nil {
		return fmt.Errorf("vault health check failed: %w", err)
	}
	if health.Sealed {
		return fmt.Errorf("vault is sealed")
	}
	if !health.Initialized {
		return fmt.Errorf("vault is not initialized")
	}

	return v.pingToken(ctx)
}

func (v *Vault) pingToken(ctx context.Context) error {
	v.mu.RLock()
	client := v.client
	v.mu.RUnlock()

	secret, err := client.Auth().Token().LookupSelfWithContext(ctx)
	if err != nil {
		return fmt.Errorf("vault token validation failed: %w", err)
	}
	if secret == nil || secret.Data == nil {
		return fmt.Errorf("vault token lookup returned empty response")
	}
	return nil
}

// GetSecret fetches a secret value, re-authenticating inline on a single auth
// failure rather than waiting for the background renewal goroutine to catch it.
func (v *Vault) GetSecret(ctx context.Context, path, key string) (string, error) {
	value, err := v.fetchSecret(ctx, path, key)
	if err != nil && isAuthError(err) {
		slog.Warn("vault: auth error fetching secret, re-authenticating inline")
		if _, reAuthErr := v.reAuthenticate(); reAuthErr != nil {
			return "", fmt.Errorf("vault: re-auth failed after auth error: %w", reAuthErr)
		}
		value, err = v.fetchSecret(ctx, path, key)
	}
	return value, err
}

func isAuthError(err error) bool {
	var respErr *vault.ResponseError
	return errors.As(err, &respErr) && (respErr.StatusCode == 401 || respErr.StatusCode == 403)
}

func (v *Vault) fetchSecret(ctx context.Context, path, key string) (string, error) {
	v.mu.RLock()
	client := v.client
	v.mu.RUnlock()

	mountPoint := v.config.GetMountPoint()

	var data map[string]interface{}

	switch v.config.GetKvVersion() {
	case 1:
		secret, err := client.Logical().ReadWithContext(ctx, fmt.Sprintf("%s/%s", mountPoint, path))
		if err != nil {
			return "", fmt.Errorf("failed to read KV v1 secret at %q: %w", path, err)
		}
		if secret == nil || secret.Data == nil {
			return "", fmt.Errorf("secret not found at %q", path)
		}
		data = secret.Data

	case 2:
		secret, err := client.KVv2(mountPoint).Get(ctx, path)
		if err != nil {
			return "", fmt.Errorf("failed to read KV v2 secret at %q: %w", path, err)
		}
		if secret == nil || secret.Data == nil {
			return "", fmt.Errorf("secret not found at %q", path)
		}
		data = secret.Data

	default:
		return "", fmt.Errorf("unsupported KV version: %d", v.config.GetKvVersion())
	}

	value, ok := data[key].(string)
	if !ok {
		return "", fmt.Errorf("key %q not found or not a string in secret %q", key, path)
	}

	return value, nil
}

func (v *Vault) watchTokenLifetime(secret *vault.Secret) {
	defer close(v.watcherDone)

	// Use the proto Duration field for the renewal increment.
	// protoutil.DurationOrZero returns 0 when not set, which tells the
	// LifetimeWatcher to use Vault's default TTL increment.
	incrementSeconds := int(protoutil.DurationOrZero(v.config.GetTimeout()).Seconds())

	for {
		v.mu.RLock()
		client := v.client
		v.mu.RUnlock()

		watcher, err := client.NewLifetimeWatcher(&vault.LifetimeWatcherInput{
			Secret:    secret,
			Increment: incrementSeconds,
		})
		if err != nil {
			slog.Error("vault: failed to create LifetimeWatcher, will retry in 30s", "error", err)
			select {
			case <-v.renewStopCh:
				return
			case <-time.After(30 * time.Second):
				continue
			}
		}

		go watcher.Start()

		select {
		case <-v.renewStopCh:
			watcher.Stop()
			return

		case renewal := <-watcher.RenewCh():
			slog.Info("vault: token renewed", "new_ttl", renewal.Secret.Auth.LeaseDuration)
			secret = renewal.Secret
			watcher.Stop()

		case err := <-watcher.DoneCh():
			watcher.Stop()
			if err != nil {
				slog.Warn("vault: token watcher done with error, re-authenticating", "error", err)
			} else {
				slog.Info("vault: token TTL exhausted, re-authenticating")
			}

			newSecret, reAuthErr := v.reAuthenticate()
			if reAuthErr != nil {
				// Use proto retry_backoff if configured, else default 30s.
				backoff := protoutil.DurationOrDefault(v.config.GetRetryBackoff(), 30*time.Second)
				slog.Error("vault: re-authentication failed, will retry",
					"error", reAuthErr, "backoff", backoff)
				select {
				case <-v.renewStopCh:
					return
				case <-time.After(backoff):
				}
				continue
			}

			if newSecret != nil && newSecret.Auth != nil {
				secret = newSecret
			} else {
				go v.pollStaticToken()
				return
			}
		}
	}
}

func (v *Vault) pollStaticToken() {
	defer close(v.watcherDone)

	ticker := time.NewTicker(30 * time.Second)
	defer ticker.Stop()

	for {
		select {
		case <-v.renewStopCh:
			return
		case <-ticker.C:
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			err := v.pingToken(ctx)
			cancel()
			if err != nil {
				slog.Error("vault: static token validation failed", "error", err)
			}
		}
	}
}

func (v *Vault) reAuthenticate() (*vault.Secret, error) {
	// Use proto timeout field for the re-auth deadline.
	timeout := protoutil.DurationOrDefault(v.config.GetTimeout(), 30*time.Second)
	ctx, cancel := context.WithTimeout(context.Background(), timeout)
	defer cancel()

	v.mu.RLock()
	client := v.client
	v.mu.RUnlock()

	secret, err := v.authenticateWithClient(ctx, client)
	if err != nil {
		return nil, fmt.Errorf("re-authentication failed: %w", err)
	}

	slog.Info("vault: re-authentication successful")
	return secret, nil
}

func (v *Vault) buildClientWithSecret(ctx context.Context) (*vault.Client, *vault.Secret, error) {
	cfg := vault.DefaultConfig()
	cfg.Address = v.config.Endpoint

	// No global timeout on the HTTP client — individual callers supply context deadlines.
	cfg.Timeout = 0

	tlsConfig, err := v.buildTLSConfig()
	if err != nil {
		return nil, nil, fmt.Errorf("failed to build TLS config: %w", err)
	}
	cfg.HttpClient = &http.Client{
		Transport: &http.Transport{TLSClientConfig: tlsConfig},
	}

	client, err := vault.NewClient(cfg)
	if err != nil {
		return nil, nil, fmt.Errorf("failed to create vault client: %w", err)
	}

	if ns := v.config.GetNamespace(); ns != "" {
		client.SetNamespace(ns)
	}

	secret, err := v.authenticateWithClient(ctx, client)
	if err != nil {
		return nil, nil, fmt.Errorf("authentication failed: %w", err)
	}

	return client, secret, nil
}

func (v *Vault) buildTLSConfig() (*tls.Config, error) {
	tlsCfg := &tls.Config{
		InsecureSkipVerify: !v.config.GetVerifySsl(), //nolint:gosec
	}

	if ca := v.config.GetSslCa(); ca != "" {
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM([]byte(ca)) {
			return nil, fmt.Errorf("failed to parse vault CA certificate")
		}
		tlsCfg.RootCAs = pool
	}

	cert := v.config.GetSslCert()
	key := v.config.GetSslKey()
	if cert != "" && key != "" {
		keypair, err := tls.X509KeyPair([]byte(cert), []byte(key))
		if err != nil {
			return nil, fmt.Errorf("failed to load vault client certificate: %w", err)
		}
		tlsCfg.Certificates = []tls.Certificate{keypair}
	}

	return tlsCfg, nil
}

func (v *Vault) authenticateWithClient(ctx context.Context, client *vault.Client) (*vault.Secret, error) {
	switch auth := v.config.Auth.(type) {
	case *pb_vault.VaultConfig_TokenAuth:
		return nil, v.authToken(client, auth.TokenAuth)
	case *pb_vault.VaultConfig_AppRoleAuth:
		return v.authAppRole(ctx, client, auth.AppRoleAuth)
	case *pb_vault.VaultConfig_KubernetesAuth:
		return v.authKubernetes(ctx, client, auth.KubernetesAuth)
	case *pb_vault.VaultConfig_UserpassAuth:
		return v.authUserPass(ctx, client, auth.UserpassAuth)
	default:
		return nil, fmt.Errorf("unsupported vault auth type: %T", v.config.Auth)
	}
}

func (v *Vault) authToken(client *vault.Client, auth *pb_vault.TokenAuth) error {
	if auth.GetToken() == "" {
		return fmt.Errorf("vault token auth: token is empty")
	}
	client.SetToken(auth.GetToken())
	return nil
}

func (v *Vault) authAppRole(ctx context.Context, client *vault.Client, auth *pb_vault.AppRoleAuth) (*vault.Secret, error) {
	secretID := &approle.SecretID{}
	switch auth.GetSecretIdType() {
	case pb_enums.SecretIDType_PLAIN:
		secretID.FromString = auth.GetSecretId()
	case pb_enums.SecretIDType_ENVIRONMENT:
		secretID.FromEnv = auth.GetSecretId()
	default:
		return nil, fmt.Errorf("unsupported approle secret_id_type: %v", auth.GetSecretIdType())
	}

	var opts []approle.LoginOption
	if mp := auth.GetMountPath(); mp != "" {
		opts = append(opts, approle.WithMountPath(mp))
	}

	appRoleAuth, err := approle.NewAppRoleAuth(auth.GetRoleId(), secretID, opts...)
	if err != nil {
		return nil, fmt.Errorf("failed to create approle auth: %w", err)
	}

	resp, err := client.Auth().Login(ctx, appRoleAuth)
	if err != nil {
		return nil, fmt.Errorf("failed to login with approle: %w", err)
	}
	if resp == nil || resp.Auth == nil {
		return nil, fmt.Errorf("vault approle: empty auth response")
	}

	client.SetToken(resp.Auth.ClientToken)
	return resp, nil
}

func (v *Vault) authKubernetes(ctx context.Context, client *vault.Client, auth *pb_vault.KubernetesAuth) (*vault.Secret, error) {
	jwtPath := auth.GetJwtPath()
	if jwtPath == "" {
		jwtPath = "/var/run/secrets/kubernetes.io/serviceaccount/token"
	}

	jwt, err := os.ReadFile(jwtPath)
	if err != nil {
		return nil, fmt.Errorf("failed to read kubernetes JWT from %s: %w", jwtPath, err)
	}

	opts := []authk8s.LoginOption{authk8s.WithServiceAccountToken(string(jwt))}
	if mp := auth.GetMountPath(); mp != "" {
		opts = append(opts, authk8s.WithMountPath(mp))
	}

	k8sAuth, err := authk8s.NewKubernetesAuth(auth.GetRole(), opts...)
	if err != nil {
		return nil, fmt.Errorf("failed to create kubernetes auth: %w", err)
	}

	resp, err := client.Auth().Login(ctx, k8sAuth)
	if err != nil {
		return nil, fmt.Errorf("failed to login with kubernetes: %w", err)
	}
	if resp == nil || resp.Auth == nil {
		return nil, fmt.Errorf("vault kubernetes: empty auth response")
	}

	client.SetToken(resp.Auth.ClientToken)
	return resp, nil
}

func (v *Vault) authUserPass(ctx context.Context, client *vault.Client, auth *pb_vault.UserPassAuth) (*vault.Secret, error) {
	var opts []authuserpass.LoginOption
	if mp := auth.GetMountPath(); mp != "" {
		opts = append(opts, authuserpass.WithMountPath(mp))
	}

	userpassAuth, err := authuserpass.NewUserpassAuth(
		auth.GetUsername(),
		&authuserpass.Password{FromString: auth.GetPassword()},
		opts...,
	)
	if err != nil {
		return nil, fmt.Errorf("failed to create userpass auth: %w", err)
	}

	resp, err := client.Auth().Login(ctx, userpassAuth)
	if err != nil {
		return nil, fmt.Errorf("failed to login with userpass: %w", err)
	}
	if resp == nil || resp.Auth == nil {
		return nil, fmt.Errorf("vault userpass: empty auth response")
	}

	client.SetToken(resp.Auth.ClientToken)
	return resp, nil
}
