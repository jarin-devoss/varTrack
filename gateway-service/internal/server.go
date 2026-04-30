package internal

import (
	"context"
	"crypto/ecdsa"
	"crypto/elliptic"
	"crypto/rand"
	"crypto/tls"
	"crypto/x509"
	"crypto/x509/pkix"
	"fmt"
	"log/slog"
	"math/big"
	"net"
	"net/http"
	"os"
	"time"
)

// TLSConfig holds inbound TLS settings for the HTTP server.
type TLSConfig struct {
	CertFile string // Path to PEM-encoded certificate.
	KeyFile  string // Path to PEM-encoded private key.

	MinVersion uint16 // Default: tls.VersionTLS12.

	// SelfSignedIfMissing generates a self-signed cert when true and
	// cert/key files are not provided. Useful for local development.
	SelfSignedIfMissing bool
}

// Enabled returns true when TLS should be used.
func (t *TLSConfig) Enabled() bool {
	if t == nil {
		return false
	}
	return (t.CertFile != "" && t.KeyFile != "") || t.SelfSignedIfMissing
}

func Run(ctx context.Context, addr string, handler http.Handler, tlsCfg *TLSConfig) {
	srv := &http.Server{
		Addr:              addr,
		Handler:           handler,
		ReadHeaderTimeout: 10 * time.Second,
		ReadTimeout:       30 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout: 60 * time.Second,
	}

	useTLS := tlsCfg != nil && tlsCfg.Enabled()

	if useTLS {
		tlsServerConfig, err := buildServerTLSConfig(tlsCfg)
		if err != nil {
			slog.Error("failed to build TLS config", "error", err)
			os.Exit(1)
		}
		srv.TLSConfig = tlsServerConfig
	}

	// Graceful shutdown with 20s drain timeout.
	done := make(chan struct{})
	go func() {
		<-ctx.Done()
		slog.Info("shutting down server")
		shutdownCtx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
		defer cancel()
		if err := srv.Shutdown(shutdownCtx); err != nil {
			slog.Error("server shutdown error", "error", err)
		}
		close(done)
	}()

	slog.Info("server starting", "addr", addr, "tls", useTLS)

	var err error
	if useTLS {
		err = srv.ListenAndServeTLS(tlsCfg.CertFile, tlsCfg.KeyFile)
	} else {
		err = srv.ListenAndServe()
	}

	<-done

	if err != nil {
		if err == http.ErrServerClosed {
			slog.Info("server stopped gracefully")
		} else {
			slog.Error("server error", "error", err)
			os.Exit(1)
		}
	}
}

// buildServerTLSConfig constructs a *tls.Config for the HTTP server.
func buildServerTLSConfig(cfg *TLSConfig) (*tls.Config, error) {
	tc := &tls.Config{
		MinVersion: cfg.MinVersion,
		CipherSuites: []uint16{
			tls.TLS_ECDHE_ECDSA_WITH_AES_128_GCM_SHA256,
			tls.TLS_ECDHE_RSA_WITH_AES_128_GCM_SHA256,
			tls.TLS_ECDHE_ECDSA_WITH_AES_256_GCM_SHA384,
			tls.TLS_ECDHE_RSA_WITH_AES_256_GCM_SHA384,
			tls.TLS_ECDHE_ECDSA_WITH_CHACHA20_POLY1305_SHA256,
			tls.TLS_ECDHE_RSA_WITH_CHACHA20_POLY1305_SHA256,
		},
	}
	if tc.MinVersion == 0 {
		tc.MinVersion = tls.VersionTLS12
	}

	hasCert := cfg.CertFile != ""
	hasKey := cfg.KeyFile != ""

	switch {
	case hasCert && hasKey:
		cert, err := tls.LoadX509KeyPair(cfg.CertFile, cfg.KeyFile)
		if err != nil {
			return nil, fmt.Errorf("failed to load TLS keypair (cert=%s, key=%s): %w",
				cfg.CertFile, cfg.KeyFile, err)
		}
		tc.Certificates = []tls.Certificate{cert}
		slog.Info("loaded TLS certificate from files",
			"cert", cfg.CertFile, "key", cfg.KeyFile)

	case cfg.SelfSignedIfMissing:
		cert, err := generateSelfSignedCert()
		if err != nil {
			return nil, fmt.Errorf("failed to generate self-signed cert: %w", err)
		}
		tc.Certificates = []tls.Certificate{cert}
		slog.Warn("using auto-generated self-signed TLS certificate (not for production)")

	default:
		return nil, fmt.Errorf("TLS enabled but no cert/key provided and self-signed fallback is disabled")
	}

	return tc, nil
}

// generateSelfSignedCert creates a self-signed ECDSA P-256 certificate
// valid for localhost, 127.0.0.1, and the current pod/host IP, lasting 24 hours.
func generateSelfSignedCert() (tls.Certificate, error) {
	key, err := ecdsa.GenerateKey(elliptic.P256(), rand.Reader)
	if err != nil {
		return tls.Certificate{}, err
	}

	serial, err := rand.Int(rand.Reader, new(big.Int).Lsh(big.NewInt(1), 128))
	if err != nil {
		return tls.Certificate{}, err
	}

	ipAddresses := []net.IP{net.IPv4(127, 0, 0, 1), net.IPv6loopback}
	if addrs, err := net.InterfaceAddrs(); err == nil {
		for _, addr := range addrs {
			if ipNet, ok := addr.(*net.IPNet); ok && !ipNet.IP.IsLoopback() {
				if ip4 := ipNet.IP.To4(); ip4 != nil {
					ipAddresses = append(ipAddresses, ip4)
				} else if ip6 := ipNet.IP.To16(); ip6 != nil {
					ipAddresses = append(ipAddresses, ip6)
				}
			}
		}
	}

	// Also check POD_IP env var (Kubernetes downward API)
	if podIP := os.Getenv("POD_IP"); podIP != "" {
		if ip := net.ParseIP(podIP); ip != nil {
			ipAddresses = append(ipAddresses, ip)
		}
	}

	template := x509.Certificate{
		SerialNumber: serial,
		Subject:      pkix.Name{Organization: []string{"gateway-service (self-signed)"}},
		NotBefore:    time.Now(),
		NotAfter:     time.Now().Add(24 * time.Hour),
		KeyUsage:     x509.KeyUsageKeyEncipherment | x509.KeyUsageDigitalSignature,
		ExtKeyUsage:  []x509.ExtKeyUsage{x509.ExtKeyUsageServerAuth},
		DNSNames:     []string{"localhost"},
		IPAddresses:  ipAddresses,
	}

	certDER, err := x509.CreateCertificate(rand.Reader, &template, &template, &key.PublicKey, key)
	if err != nil {
		return tls.Certificate{}, err
	}

	return tls.Certificate{
		Certificate: [][]byte{certDER},
		PrivateKey:  key,
	}, nil
}
