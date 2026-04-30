package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"log/slog"
	"os"
	"sync"
	"time"

	"gateway-service/internal/config"

	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/credentials/insecure"
)

// rotatingKeyPair holds a cached TLS certificate and the file paths it
// was loaded from. It is replaced atomically when the files change.
type rotatingKeyPair struct {
	mu       sync.RWMutex
	cert     *tls.Certificate
	certFile string
	keyFile  string
}

// load reads the cert and key from disk and updates the cached certificate.
func (rkp *rotatingKeyPair) load() error {
	cert, err := tls.LoadX509KeyPair(rkp.certFile, rkp.keyFile)
	if err != nil {
		return fmt.Errorf("failed to load TLS keypair (cert=%s, key=%s): %w",
			rkp.certFile, rkp.keyFile, err)
	}

	rkp.mu.Lock()
	rkp.cert = &cert
	rkp.mu.Unlock()

	slog.Info("gRPC client certificate reloaded",
		"cert", rkp.certFile,
		"key", rkp.keyFile,
	)
	return nil
}

// get returns the current certificate. Safe for concurrent use.
func (rkp *rotatingKeyPair) get(_ *tls.CertificateRequestInfo) (*tls.Certificate, error) {
	rkp.mu.RLock()
	defer rkp.mu.RUnlock()

	if rkp.cert == nil {
		return &tls.Certificate{}, nil
	}
	return rkp.cert, nil
}

// buildTransportCredentials returns TLS credentials for the outbound gRPC
// connection to the orchestrator, with support for certificate rotation.
//
// Uses GetClientCertificate callback so that rotated certificates on disk
// are picked up on the next TLS handshake without a restart. A background
// goroutine also proactively reloads every 30s to avoid PEM parsing on
// the hot path.
func buildTransportCredentials(env *config.Env) (credentials.TransportCredentials, error) {
	if !env.IsProduction() {
		slog.Info("gRPC transport: insecure (test mode)")
		return insecure.NewCredentials(), nil
	}

	tlsCfg := &tls.Config{}

	// Build root CA pool.
	if env.GRPCTlsCa != "" {
		caPEM, err := os.ReadFile(env.GRPCTlsCa)
		if err != nil {
			return nil, fmt.Errorf("failed to read CA cert %s: %w", env.GRPCTlsCa, err)
		}
		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(caPEM) {
			return nil, fmt.Errorf("failed to parse CA certificate from %s", env.GRPCTlsCa)
		}
		tlsCfg.RootCAs = pool
	} else {
		pool, _ := x509.SystemCertPool()
		if pool == nil {
			pool = x509.NewCertPool()
		}
		tlsCfg.RootCAs = pool
	}

	// mTLS client certificate — use rotating callback instead of static load.
	if env.GRPCTlsCert != "" && env.GRPCTlsKey != "" {
		rkp := &rotatingKeyPair{
			certFile: env.GRPCTlsCert,
			keyFile:  env.GRPCTlsKey,
		}

		if err := rkp.load(); err != nil {
			return nil, fmt.Errorf("failed to load initial client TLS keypair: %w", err)
		}

		tlsCfg.GetClientCertificate = rkp.get

		// Background reloader: proactively refreshes the in-memory cert.
		go func() {
			ticker := time.NewTicker(30 * time.Second)
			defer ticker.Stop()
			for range ticker.C {
				if err := rkp.load(); err != nil {
					slog.Warn("gRPC client cert reload failed (will retry)",
						"cert", env.GRPCTlsCert,
						"key", env.GRPCTlsKey,
						"error", err,
					)
				}
			}
		}()

		slog.Info("gRPC transport: mTLS with certificate rotation enabled",
			"cert", env.GRPCTlsCert,
		)
	} else {
		slog.Info("gRPC transport: TLS (no client cert — server-side TLS only)")
	}

	return credentials.NewTLS(tlsCfg), nil
}

// shutdownGRPCConnection drains in-flight gRPC calls before closing the
// connection. Called during graceful shutdown.
func shutdownGRPCConnection(ctx context.Context, conn interface{ Close() error }) error {
	done := make(chan error, 1)
	go func() {
		done <- conn.Close()
	}()

	select {
	case err := <-done:
		if err != nil {
			return fmt.Errorf("gRPC connection close error: %w", err)
		}
		slog.Info("gRPC connection closed gracefully")
		return nil
	case <-ctx.Done():
		slog.Warn("gRPC connection drain timed out; forcing close")
		return ctx.Err()
	}
}
