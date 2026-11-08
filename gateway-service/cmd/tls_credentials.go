package main

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"log/slog"
	"net"
	"os"
	"sync"
	"time"

	"gateway-service/internal/authz"
	"gateway-service/internal/config"
	"gateway-service/internal/models"

	"google.golang.org/grpc"
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

// checkPlatformAPIs logs, for each platform that can say whether its API is the one its type
// names, when it is not. It never stops the gateway: webhooks work without it.
func checkPlatformAPIs(ctx context.Context, bundle *models.Bundle) {
	checkCtx, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	for _, name := range bundle.PlatformNames() {
		platform, err := bundle.GetPlatform(checkCtx, name, "")
		if err != nil {
			continue
		}
		checker, ok := platform.(interface{ CheckAPI(context.Context) error })
		if !ok {
			continue
		}
		if err := checker.CheckAPI(checkCtx); err != nil {
			slog.Warn("gateway: platform "+name+": "+err.Error(), "platform", name)
		}
	}
}

// startAuthzServer serves the permission engine on AUTHZ_ADDR, for the orchestrator's
// web UI. It returns nil when AUTHZ_ADDR is unset.
//
// In production the listener is TLS with the gateway's own certificate and, when
// GRPC_TLS_CA is set, demands a client certificate that CA signed (mTLS), like the
// orchestrator's gRPC port; every call must in any case carry a signed "authz" grant.
func startAuthzServer(env *config.Env, engine *authz.Engine) (*grpc.Server, error) {
	if env.AuthzAddr == "" || engine == nil {
		return nil, nil
	}
	srv := authz.NewServer(engine, env.AuthzSecret)
	opts := []grpc.ServerOption{srv.ServerOption()}
	switch {
	case env.IsProduction():
		cert, err := tls.LoadX509KeyPair(env.GatewayTlsCert, env.GatewayTlsKey)
		if err != nil {
			return nil, fmt.Errorf("authz server: load TLS keypair: %w", err)
		}
		tlsCfg := &tls.Config{Certificates: []tls.Certificate{cert}, MinVersion: tls.VersionTLS12}
		if env.GRPCTlsCa != "" {
			caPEM, err := os.ReadFile(env.GRPCTlsCa)
			if err != nil {
				return nil, fmt.Errorf("authz server: read CA %s: %w", env.GRPCTlsCa, err)
			}
			pool := x509.NewCertPool()
			if !pool.AppendCertsFromPEM(caPEM) {
				return nil, fmt.Errorf("authz server: no certificate in %s", env.GRPCTlsCa)
			}
			tlsCfg.ClientCAs = pool
			tlsCfg.ClientAuth = tls.RequireAndVerifyClientCert
		}
		opts = append(opts, grpc.Creds(credentials.NewTLS(tlsCfg)))
	case env.AuthzSecret == "":
		slog.Warn("gateway: the permission engine is served WITHOUT authentication and without TLS " +
			"(no INTERNAL_AUTH_SECRET): anyone who can reach AUTHZ_ADDR can ask it about a token. Development only.")
	}
	lis, err := net.Listen("tcp", env.AuthzAddr)
	if err != nil {
		return nil, fmt.Errorf("authz server: listen on %s: %w", env.AuthzAddr, err)
	}
	gs := grpc.NewServer(opts...)
	srv.Register(gs)
	go func() {
		if err := gs.Serve(lis); err != nil {
			slog.Error("authz server stopped", "error", err)
		}
	}()
	slog.Info("gateway: permission engine served", "addr", env.AuthzAddr, "signed_grants", env.AuthzSecret != "")
	return gs, nil
}
