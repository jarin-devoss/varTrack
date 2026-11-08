package config

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"os"
	"strings"

	"github.com/redis/go-redis/v9"

	"gateway-service/internal/models"
)

// NonceRedisOptions are the connection settings of the Redis datasource config.cue names as
// gateway_nonce_datasource, built field by field from the bundle the gateway already loaded: no URL is
// formatted, so no character in a password can break it, and the password may be written in any form a
// SecretRef takes (a string, {file:}, {ref:}).
//
// It returns ("", nil, nil) when no datasource is named. A datasource that is named but is not there, or
// whose password cannot be read, is an error: that is a configuration the operator asked for and did not get.
func NonceRedisOptions(ctx context.Context, b *models.Bundle) (string, *redis.Options, error) {
	name := b.GatewayNonceDatasource()
	if name == "" {
		return "", nil, nil
	}
	cfg, err := b.RedisDatasource(name)
	if err != nil {
		return name, nil, fmt.Errorf("gateway_nonce_datasource: %w", err)
	}
	if len(cfg.GetHosts()) == 0 || cfg.GetHosts()[0] == "" {
		return name, nil, fmt.Errorf("gateway_nonce_datasource: redis datasource %q has no hosts", name)
	}
	password, err := b.ResolveSecret(ctx, cfg.GetPassword())
	if err != nil {
		return name, nil, fmt.Errorf("gateway_nonce_datasource: the password of %q cannot be read: %w", name, err)
	}
	opts := &redis.Options{Addr: cfg.GetHosts()[0], Password: password, DB: int(cfg.GetDatabase())}
	if cfg.GetEnableTls() {
		tlsConfig, err := redisTLS(cfg.GetTlsCaCert(), cfg.GetInsecureSkipVerify(), opts.Addr)
		if err != nil {
			return name, nil, fmt.Errorf("gateway_nonce_datasource: tls of %q: %w", name, err)
		}
		opts.TLSConfig = tlsConfig
	}
	return name, opts, nil
}

func redisTLS(ca string, insecureSkipVerify bool, addr string) (*tls.Config, error) {
	cfg := &tls.Config{MinVersion: tls.VersionTLS12, InsecureSkipVerify: insecureSkipVerify} //nolint:gosec // the operator's explicit insecure_skip_verify
	if host, _, ok := strings.Cut(addr, ":"); ok {
		cfg.ServerName = host
	}
	if ca != "" {
		pem := []byte(ca)
		if data, err := os.ReadFile(ca); err == nil { // a path, else the PEM itself
			pem = data
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(pem) {
			return nil, fmt.Errorf("tls_ca_cert holds no certificate")
		}
		cfg.RootCAs = pool
	}
	return cfg, nil
}
