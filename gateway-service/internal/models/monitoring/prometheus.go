package monitoring

import (
	"context"
	"fmt"
	"io"
	"log/slog"
	"net"
	"net/http"
	"strconv"
	"time"

	monpb "gateway-service/internal/gen/proto/go/vartrack/v1/models/monitoring"
	"gateway-service/internal/protoutil"

	otelprom "go.opentelemetry.io/otel/exporters/prometheus"
	"go.opentelemetry.io/otel/metric"
	sdkmetric "go.opentelemetry.io/otel/sdk/metric"

	"github.com/prometheus/client_golang/prometheus"
	"github.com/prometheus/client_golang/prometheus/collectors"
	"github.com/prometheus/client_golang/prometheus/promhttp"
	"github.com/prometheus/client_golang/prometheus/push"
)

// PrometheusBackend exposes a /metrics scrape endpoint and optionally pushes
// to a Pushgateway.
//
// The OTel→Prometheus bridge exporter is used so every AppMetrics instrument
// (counter, histogram, gauge) is automatically visible at the scrape endpoint —
// no manual prometheus.Register calls required anywhere in the app.
//
// Implements:
//   - Backend            (Name / Ping / Shutdown)
//   - meterProviderBackend  (MeterProvider)
type PrometheusBackend struct {
	cfg      *monpb.PrometheusConfig
	registry *prometheus.Registry
	provider *sdkmetric.MeterProvider
	server   *http.Server
	pusher   *push.Pusher
	pushStop chan struct{}
}

// NewPrometheusBackend creates the Prometheus registry, OTel MeterProvider,
// and starts the HTTP exposition server.
// Returns a disabled no-op backend (not an error) when enabled: false.
func NewPrometheusBackend(ctx context.Context, cfg *monpb.PrometheusConfig) (*PrometheusBackend, error) {
	b := &PrometheusBackend{
		cfg:      cfg,
		pushStop: make(chan struct{}),
	}

	if !cfg.GetEnabled() {
		slog.Info("prometheus: disabled")
		return b, nil
	}

	// Dedicated registry — never collides with the global default registerer.
	reg := prometheus.NewRegistry()
	reg.MustRegister(
		// MetricsAll captures all /sched, /gc, /memory etc. runtime metrics.
		collectors.NewGoCollector(
			collectors.WithGoCollectorRuntimeMetrics(collectors.MetricsAll),
		),
		collectors.NewProcessCollector(collectors.ProcessCollectorOpts{}),
	)
	b.registry = reg

	res, err := buildOTelResource(ctx, cfg.GetJobName(), "", "", cfg.GetExternalLabels())
	if err != nil {
		return nil, fmt.Errorf("prometheus: resource: %w", err)
	}

	// OTel→Prometheus bridge: registers OTel instruments into our dedicated registry.
	promExp, err := otelprom.New(otelprom.WithRegisterer(reg))
	if err != nil {
		return nil, fmt.Errorf("prometheus: OTel exporter: %w", err)
	}

	b.provider = sdkmetric.NewMeterProvider(
		sdkmetric.WithReader(promExp),
		sdkmetric.WithResource(res),
	)

	if port := cfg.GetPort(); port > 0 {
		if err := b.startServer(cfg); err != nil {
			return nil, fmt.Errorf("prometheus: start server: %w", err)
		}
	}

	if cfg.GetPushEnabled() {
		if err := b.startPushLoop(cfg); err != nil {
			return nil, fmt.Errorf("prometheus: start push loop: %w", err)
		}
	}

	slog.Info("prometheus: started",
		"port", cfg.GetPort(),
		"path", cfg.GetMetricsPath(),
		"push_enabled", cfg.GetPushEnabled(),
	)
	return b, nil
}

func (b *PrometheusBackend) Name() string { return "prometheus" }

// MeterProvider satisfies meterProviderBackend.
// monitoring.Init() installs this as the global OTel MeterProvider.
func (b *PrometheusBackend) MeterProvider() metric.MeterProvider {
	if b.provider == nil {
		return nil
	}
	return b.provider
}

// Ping does a live GET /metrics against the local exposition server.
// A failure means the server goroutine has died — worth logging, not worth
// stopping traffic over.
func (b *PrometheusBackend) Ping(ctx context.Context) error {
	if b.server == nil {
		return nil
	}

	metricsPath := b.cfg.GetMetricsPath()
	if metricsPath == "" {
		metricsPath = "/metrics"
	}
	scheme := "http"
	if b.cfg.GetEnableTls() {
		scheme = "https"
	}
	url := scheme + "://" + b.server.Addr + metricsPath

	reqCtx, cancel := context.WithTimeout(ctx, 3*time.Second)
	defer cancel()

	req, err := http.NewRequestWithContext(reqCtx, http.MethodGet, url, nil)
	if err != nil {
		return fmt.Errorf("prometheus ping: %w", err)
	}
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return fmt.Errorf("prometheus ping GET %s: %w", url, err)
	}
	io.Copy(io.Discard, resp.Body)
	resp.Body.Close()

	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("prometheus ping: %s returned HTTP %d", url, resp.StatusCode)
	}
	return nil
}

// Shutdown stops the Pushgateway loop, the HTTP server, and the MeterProvider.
func (b *PrometheusBackend) Shutdown(ctx context.Context) error {
	var errs []error

	if b.provider != nil {
		if err := b.provider.Shutdown(ctx); err != nil {
			errs = append(errs, err)
		}
	}

	// Signal push loop goroutine to stop.
	if b.pushStop != nil {
		select {
		case <-b.pushStop: // already closed
		default:
			close(b.pushStop)
		}
	}

	if b.server != nil {
		if err := b.server.Shutdown(ctx); err != nil {
			errs = append(errs, err)
		}
	}

	if len(errs) > 0 {
		return fmt.Errorf("prometheus shutdown: %v", errs)
	}
	slog.Info("prometheus: shut down")
	return nil
}

// ── HTTP exposition server ────────────────────────────────────────────────────

func (b *PrometheusBackend) startServer(cfg *monpb.PrometheusConfig) error {
	metricsPath := cfg.GetMetricsPath()
	if metricsPath == "" {
		metricsPath = "/metrics"
	}

	handler := promhttp.HandlerFor(b.registry, promhttp.HandlerOpts{
		Registry:          b.registry,
		EnableOpenMetrics: true, // enables content negotiation for OpenMetrics format
	})

	if user := cfg.GetBasicAuthUser(); user != "" {
		handler = basicAuthMiddleware(user, cfg.GetBasicAuthPass(), handler)
	}

	mux := http.NewServeMux()
	mux.Handle(metricsPath, handler)
	// /healthz on the same port lets k8s liveness probes reuse it cheaply.
	mux.HandleFunc("/healthz", func(w http.ResponseWriter, _ *http.Request) {
		w.WriteHeader(http.StatusOK)
		w.Write([]byte("OK"))
	})

	addr := net.JoinHostPort("", strconv.Itoa(int(cfg.GetPort())))
	srv := &http.Server{
		Addr:              addr,
		Handler:           mux,
		ReadHeaderTimeout: 5 * time.Second,
		ReadTimeout:       10 * time.Second,
		WriteTimeout:      30 * time.Second,
		IdleTimeout:       60 * time.Second,
	}

	if cfg.GetEnableTls() {
		tlsCfg, err := buildBackendTLS(cfg.GetTlsCa(), cfg.GetTlsCert(), cfg.GetTlsKey(), cfg.GetInsecureSkipVerify())
		if err != nil {
			return err
		}
		srv.TLSConfig = tlsCfg
	}

	go func() {
		var serveErr error
		if cfg.GetEnableTls() {
			serveErr = srv.ListenAndServeTLS("", "") // certs already in TLSConfig
		} else {
			serveErr = srv.ListenAndServe()
		}
		if serveErr != nil && serveErr != http.ErrServerClosed {
			slog.Error("prometheus: exposition server error", "error", serveErr)
		}
	}()

	b.server = srv
	slog.Info("prometheus: exposition server started", "addr", addr, "path", metricsPath)
	return nil
}

// ── Pushgateway loop ──────────────────────────────────────────────────────────

func (b *PrometheusBackend) startPushLoop(cfg *monpb.PrometheusConfig) error {
	p := push.New(cfg.GetPushUrl(), cfg.GetJobName()).Gatherer(b.registry)
	for k, v := range cfg.GetExternalLabels() {
		p = p.Grouping(k, v)
	}
	b.pusher = p

	interval := protoutil.DurationOrDefault(cfg.GetPushInterval(), 15*time.Second)

	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for {
			select {
			case <-b.pushStop:
				return
			case <-ticker.C:
				if err := b.pusher.Push(); err != nil {
					slog.Warn("prometheus: push failed",
						"url", cfg.GetPushUrl(), "error", err)
				}
			}
		}
	}()

	slog.Info("prometheus: push loop started",
		"url", cfg.GetPushUrl(), "interval", interval)
	return nil
}

func basicAuthMiddleware(user, pass string, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		u, p, ok := r.BasicAuth()
		if !ok || u != user || p != pass {
			w.Header().Set("WWW-Authenticate", `Basic realm="metrics"`)
			http.Error(w, "Unauthorized", http.StatusUnauthorized)
			return
		}
		next.ServeHTTP(w, r)
	})
}
