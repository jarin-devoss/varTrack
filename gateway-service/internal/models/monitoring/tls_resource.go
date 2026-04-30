package monitoring

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"fmt"
	"os"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/sdk/resource"
	semconv "go.opentelemetry.io/otel/semconv/v1.26.0"
)

// buildBackendTLS constructs a *tls.Config for outbound monitoring backend
// connections (Elasticsearch, Logstash, Jaeger, OTel Collector, etc.).
//
// Parameters:
//   - caCert:             PEM-encoded CA certificate (string content or file path).
//   - clientCert:         PEM-encoded client certificate for mTLS.
//   - clientKey:          PEM-encoded client key for mTLS.
//   - insecureSkipVerify: disables server certificate verification (dev/test only).
func buildBackendTLS(caCert, clientCert, clientKey string, insecureSkipVerify bool) (*tls.Config, error) {
	cfg := &tls.Config{
		InsecureSkipVerify: insecureSkipVerify, //nolint:gosec
	}

	if caCert != "" {
		pemData := []byte(caCert)

		// If it does not look like PEM content, treat it as a file path.
		if !isPEMBlock(pemData) {
			var err error
			pemData, err = os.ReadFile(caCert)
			if err != nil {
				return nil, fmt.Errorf("read CA cert file %q: %w", caCert, err)
			}
		}

		pool := x509.NewCertPool()
		if !pool.AppendCertsFromPEM(pemData) {
			return nil, fmt.Errorf("failed to parse CA certificate")
		}
		cfg.RootCAs = pool
	}

	if clientCert != "" && clientKey != "" {
		cert, err := tls.X509KeyPair([]byte(clientCert), []byte(clientKey))
		if err != nil {
			return nil, fmt.Errorf("load client TLS keypair: %w", err)
		}
		cfg.Certificates = []tls.Certificate{cert}
	}

	return cfg, nil
}

// isPEMBlock returns true if the byte slice contains a PEM header.
func isPEMBlock(data []byte) bool {
	const header = "-----BEGIN"
	for i := range len(data) - len(header) {
		if string(data[i:i+len(header)]) == header {
			return true
		}
	}
	return false
}

// buildOTelResource constructs an OTel SDK resource with standard semantic
// conventions populated from the provided service metadata and extra attributes.
func buildOTelResource(
	ctx context.Context,
	serviceName, serviceVersion, environment string,
	extraAttrs map[string]string,
) (*resource.Resource, error) {
	kvs := []attribute.KeyValue{
		semconv.ServiceName(serviceName),
	}
	if serviceVersion != "" {
		kvs = append(kvs, semconv.ServiceVersion(serviceVersion))
	}
	if environment != "" {
		kvs = append(kvs, semconv.DeploymentEnvironment(environment))
	}
	for k, v := range extraAttrs {
		kvs = append(kvs, attribute.String(k, v))
	}

	return resource.New(ctx,
		resource.WithSchemaURL(semconv.SchemaURL),
		resource.WithAttributes(kvs...),
	)
}
