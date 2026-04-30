package monitoring

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"time"

	monpb "gateway-service/internal/gen/proto/go/vartrack/v1/models/monitoring"

	"gateway-service/internal/protoutil"
)

// ELKBackend ships structured log records to Elasticsearch (Bulk API) or
// via a Logstash HTTP input.
//
// It installs itself as a second slog handler via a multiHandler so every
// slog.Info/Warn/Error/Debug call in the application is automatically captured
// and shipped — no call-site changes required.
//
// Log flow:
//
//	slog.Info("...")
//	  → multiHandler
//	       ├─ original JSON-stdout handler  (preserved)
//	       └─ elkHandler → enqueue → flushWorker → ES Bulk API / Logstash
//
// Implements: Backend (Name / Ping / Shutdown)
type ELKBackend struct {
	cfg             *monpb.ELKConfig
	es              *esClient
	ls              *logstashWriter
	buf             []json.RawMessage
	bufMu           sync.Mutex
	flushCh         chan struct{}
	stopCh          chan struct{}
	wg              sync.WaitGroup
	previousHandler slog.Handler // restored on Shutdown
}

// NewELKBackend wires the ELK log pipeline and installs the slog multi-handler.
// Returns a disabled no-op backend (not an error) when enabled: false.
func NewELKBackend(ctx context.Context, cfg *monpb.ELKConfig) (*ELKBackend, error) {
	b := &ELKBackend{
		cfg:     cfg,
		flushCh: make(chan struct{}, 1),
		stopCh:  make(chan struct{}),
	}

	if !cfg.GetEnabled() {
		slog.Info("elk: disabled")
		return b, nil
	}

	esCfg := cfg.GetElasticsearch()
	if esCfg == nil {
		return nil, fmt.Errorf("elk: elasticsearch config is required")
	}

	es, err := newESClient(esCfg)
	if err != nil {
		return nil, fmt.Errorf("elk: build ES client: %w", err)
	}
	b.es = es

	if lsCfg := cfg.GetLogstash(); lsCfg != nil {
		ls, err := newLogstashWriter(lsCfg)
		if err != nil {
			return nil, fmt.Errorf("elk: build Logstash writer: %w", err)
		}
		b.ls = ls
	}

	// Capture current handler and fan out to both old + ELK.
	b.previousHandler = slog.Default().Handler()
	slog.SetDefault(slog.New(newMultiHandler(b.previousHandler, newELKHandler(b, cfg))))

	b.wg.Add(1)
	go b.flushWorker()

	slog.Info("elk: started",
		"service", cfg.GetServiceName(),
		"index", esCfg.GetIndex(),
		"via_logstash", cfg.GetLogstash() != nil,
	)
	return b, nil
}

func (b *ELKBackend) Name() string { return "elk" }

// Ping checks Elasticsearch (and Logstash if configured) via HTTP.
func (b *ELKBackend) Ping(ctx context.Context) error {
	if b.es == nil {
		return nil
	}
	ctx, cancel := context.WithTimeout(ctx, 5*time.Second)
	defer cancel()

	if err := b.es.ping(ctx); err != nil {
		return fmt.Errorf("elk ping (elasticsearch): %w", err)
	}
	if b.ls != nil {
		if err := b.ls.ping(ctx); err != nil {
			return fmt.Errorf("elk ping (logstash): %w", err)
		}
	}
	return nil
}

// Shutdown stops the flush worker, flushes remaining records, then restores
// the original slog handler so post-shutdown logs still appear on stdout.
func (b *ELKBackend) Shutdown(ctx context.Context) error {
	if b.stopCh != nil {
		select {
		case <-b.stopCh:
		default:
			close(b.stopCh)
		}
	}
	b.wg.Wait()

	if err := b.flush(ctx); err != nil {
		slog.Warn("elk: final flush error", "error", err)
	}

	if b.previousHandler != nil {
		slog.SetDefault(slog.New(b.previousHandler))
	}

	slog.Info("elk: shut down")
	return nil
}

// enqueue is called by elkHandler.Handle for every captured log record.
func (b *ELKBackend) enqueue(record json.RawMessage) {
	maxDocs := int(b.cfg.GetElasticsearch().GetBulkMaxDocs())
	if maxDocs == 0 {
		maxDocs = 500
	}

	b.bufMu.Lock()
	b.buf = append(b.buf, record)
	shouldFlush := len(b.buf) >= maxDocs
	b.bufMu.Unlock()

	if shouldFlush {
		select {
		case b.flushCh <- struct{}{}:
		default:
		}
	}
}

func (b *ELKBackend) flushWorker() {
	defer b.wg.Done()

	interval := protoutil.DurationOrDefault(b.cfg.GetElasticsearch().GetFlushInterval(), 5*time.Second)
	ticker := time.NewTicker(interval)
	defer ticker.Stop()

	for {
		select {
		case <-b.stopCh:
			return
		case <-b.flushCh:
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			_ = b.flush(ctx)
			cancel()
		case <-ticker.C:
			ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
			_ = b.flush(ctx)
			cancel()
		}
	}
}

func (b *ELKBackend) flush(ctx context.Context) error {
	b.bufMu.Lock()
	if len(b.buf) == 0 {
		b.bufMu.Unlock()
		return nil
	}
	batch := b.buf
	b.buf = nil
	b.bufMu.Unlock()

	if b.ls != nil {
		return b.ls.writeBatch(ctx, batch)
	}
	return b.es.bulkIndex(ctx, batch)
}

// ── slog handler ──────────────────────────────────────────────────────────────

type elkHandler struct {
	backend *ELKBackend
	cfg     *monpb.ELKConfig
	attrs   []slog.Attr
	group   string
}

func newELKHandler(backend *ELKBackend, cfg *monpb.ELKConfig) *elkHandler {
	return &elkHandler{backend: backend, cfg: cfg}
}

func (h *elkHandler) Enabled(_ context.Context, _ slog.Level) bool { return true }

func (h *elkHandler) Handle(_ context.Context, r slog.Record) error {
	doc := map[string]any{
		"@timestamp":      r.Time.UTC().Format(time.RFC3339Nano),
		"level":           r.Level.String(),
		"message":         r.Message,
		"service.name":    h.cfg.GetServiceName(),
		"service.version": h.cfg.GetServiceVersion(),
		"environment":     h.cfg.GetEnvironment(),
	}

	// Static extra fields from ES config.
	if esCfg := h.cfg.GetElasticsearch(); esCfg != nil {
		for k, v := range esCfg.GetExtraFields() {
			doc[k] = v
		}
	}

	// Handler-level attributes (added via slog.With / WithAttrs).
	for _, a := range h.attrs {
		doc[h.key(a.Key)] = a.Value.Any()
	}

	// Record-level attributes.
	r.Attrs(func(a slog.Attr) bool {
		doc[h.key(a.Key)] = a.Value.Any()
		return true
	})

	b, err := json.Marshal(doc)
	if err != nil {
		return err
	}
	h.backend.enqueue(b)
	return nil
}

func (h *elkHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	newAttrs := make([]slog.Attr, len(h.attrs)+len(attrs))
	copy(newAttrs, h.attrs)
	copy(newAttrs[len(h.attrs):], attrs)
	return &elkHandler{backend: h.backend, cfg: h.cfg, attrs: newAttrs, group: h.group}
}

func (h *elkHandler) WithGroup(name string) slog.Handler {
	return &elkHandler{backend: h.backend, cfg: h.cfg, attrs: h.attrs, group: name}
}

func (h *elkHandler) key(k string) string {
	if h.group != "" {
		return h.group + "." + k
	}
	return k
}

// ── multiHandler ──────────────────────────────────────────────────────────────

type multiHandler struct{ handlers []slog.Handler }

func newMultiHandler(handlers ...slog.Handler) *multiHandler {
	return &multiHandler{handlers: handlers}
}

func (m *multiHandler) Enabled(ctx context.Context, level slog.Level) bool {
	for _, h := range m.handlers {
		if h.Enabled(ctx, level) {
			return true
		}
	}
	return false
}

func (m *multiHandler) Handle(ctx context.Context, r slog.Record) error {
	for _, h := range m.handlers {
		if h.Enabled(ctx, r.Level) {
			if err := h.Handle(ctx, r.Clone()); err != nil {
				return err
			}
		}
	}
	return nil
}

func (m *multiHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	out := make([]slog.Handler, len(m.handlers))
	for i, h := range m.handlers {
		out[i] = h.WithAttrs(attrs)
	}
	return &multiHandler{handlers: out}
}

func (m *multiHandler) WithGroup(name string) slog.Handler {
	out := make([]slog.Handler, len(m.handlers))
	for i, h := range m.handlers {
		out[i] = h.WithGroup(name)
	}
	return &multiHandler{handlers: out}
}

// ── Elasticsearch thin client ─────────────────────────────────────────────────

type esClient struct {
	http       *http.Client
	endpoints  []string
	index      string
	authHeader string
	pipeline   string
}

func newESClient(cfg *monpb.ElasticsearchConfig) (*esClient, error) {
	tlsCfg, err := buildBackendTLS(
		cfg.GetTlsCaCert(), cfg.GetTlsClientCert(), cfg.GetTlsClientKey(),
		cfg.GetInsecureSkipVerify(),
	)
	if err != nil {
		return nil, err
	}

	authHeader := ""
	switch {
	case cfg.GetApiKey() != "":
		authHeader = "ApiKey " + cfg.GetApiKey()
	case cfg.GetBearerToken() != "":
		authHeader = "Bearer " + cfg.GetBearerToken()
	case cfg.GetUsername() != "":
		req, _ := http.NewRequest(http.MethodGet, "", nil)
		req.SetBasicAuth(cfg.GetUsername(), cfg.GetPassword())
		authHeader = req.Header.Get("Authorization")
	}

	return &esClient{
		http: &http.Client{
			Transport: &http.Transport{TLSClientConfig: tlsCfg},
			Timeout:   30 * time.Second,
		},
		endpoints:  cfg.GetEndpoints(),
		index:      cfg.GetIndex(),
		authHeader: authHeader,
		pipeline:   cfg.GetPipeline(),
	}, nil
}

func (c *esClient) ping(ctx context.Context) error {
	if len(c.endpoints) == 0 {
		return fmt.Errorf("no elasticsearch endpoints configured")
	}
	url := strings.TrimSuffix(c.endpoints[0], "/") + "/_cluster/health"
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, url, nil)
	if err != nil {
		return err
	}
	if c.authHeader != "" {
		req.Header.Set("Authorization", c.authHeader)
	}
	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("elasticsearch unreachable: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 500 {
		return fmt.Errorf("elasticsearch cluster health: %s", resp.Status)
	}
	return nil
}

func (c *esClient) bulkIndex(ctx context.Context, records []json.RawMessage) error {
	if len(records) == 0 {
		return nil
	}

	var buf bytes.Buffer
	for _, rec := range records {
		// Each Bulk API action requires an action meta line followed by the document.
		meta := map[string]any{
			"index": map[string]any{"_index": c.index},
		}
		if err := json.NewEncoder(&buf).Encode(meta); err != nil {
			return err
		}
		buf.Write(rec)
		buf.WriteByte('\n')
	}

	url := strings.TrimSuffix(c.endpoints[0], "/") + "/_bulk"
	if c.pipeline != "" {
		url += "?pipeline=" + c.pipeline
	}

	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, &buf)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/x-ndjson")
	if c.authHeader != "" {
		req.Header.Set("Authorization", c.authHeader)
	}

	resp, err := c.http.Do(req)
	if err != nil {
		return fmt.Errorf("elasticsearch bulk: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return fmt.Errorf("elasticsearch bulk returned %s", resp.Status)
	}
	return nil
}

// ── Logstash HTTP writer ──────────────────────────────────────────────────────

type logstashWriter struct {
	http *http.Client
	cfg  *monpb.LogstashConfig
}

func newLogstashWriter(cfg *monpb.LogstashConfig) (*logstashWriter, error) {
	tlsCfg, err := buildBackendTLS(
		cfg.GetTlsCaCert(), cfg.GetTlsClientCert(), cfg.GetTlsClientKey(),
		cfg.GetInsecureSkipVerify(),
	)
	if err != nil {
		return nil, err
	}
	timeout := protoutil.DurationOrDefault(cfg.GetTimeout(), 10*time.Second)
	return &logstashWriter{
		cfg: cfg,
		http: &http.Client{
			Transport: &http.Transport{TLSClientConfig: tlsCfg},
			Timeout:   timeout,
		},
	}, nil
}

func (lw *logstashWriter) ping(ctx context.Context) error {
	scheme := "http"
	if lw.cfg.GetUseTls() {
		scheme = "https"
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodGet,
		scheme+"://"+lw.cfg.GetEndpoint(), nil)
	if err != nil {
		return err
	}
	resp, err := lw.http.Do(req)
	if err != nil {
		return fmt.Errorf("logstash unreachable: %w", err)
	}
	defer resp.Body.Close()
	return nil
}

func (lw *logstashWriter) writeBatch(ctx context.Context, records []json.RawMessage) error {
	var buf bytes.Buffer
	for _, rec := range records {
		buf.Write(rec)
		buf.WriteByte('\n')
	}

	scheme := "http"
	if lw.cfg.GetUseTls() {
		scheme = "https"
	}
	req, err := http.NewRequestWithContext(ctx, http.MethodPost,
		scheme+"://"+lw.cfg.GetEndpoint(), &buf)
	if err != nil {
		return err
	}
	req.Header.Set("Content-Type", "application/x-ndjson")

	resp, err := lw.http.Do(req)
	if err != nil {
		return fmt.Errorf("logstash write: %w", err)
	}
	defer resp.Body.Close()
	if resp.StatusCode >= 300 {
		return fmt.Errorf("logstash returned %s", resp.Status)
	}
	return nil
}

func (lw *logstashWriter) close() {}
