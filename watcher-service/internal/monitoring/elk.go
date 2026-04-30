package monitoring

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"sync"
	"time"
)

// InitELK installs an Elasticsearch log handler alongside the existing stdout
// handler. Every slog record is shipped to the ES Bulk API.
// Returns a flush+shutdown function the caller must invoke on exit.
func InitELK(endpoints []string, index, serviceName, serviceVersion, environment string) (shutdown func()) {
	if len(endpoints) == 0 {
		return func() {}
	}

	st := &elkState{
		stopCh:  make(chan struct{}),
		flushCh: make(chan struct{}, 1),
	}
	h := &elkHandler{
		endpoint:   endpoints[0],
		index:      index,
		svcName:    serviceName,
		svcVersion: serviceVersion,
		env:        environment,
		state:      st,
	}

	// Replace default slog handler with a multiHandler (stdout + ELK).
	existing := slog.Default().Handler()
	multi := &multiSlogHandler{handlers: []slog.Handler{existing, h}}
	slog.SetDefault(slog.New(multi))

	go h.flushWorker()

	slog.Info("elk: log handler initialised", "endpoint", endpoints[0], "index", index)

	return func() {
		close(st.stopCh)
		h.flush() // final flush
	}
}

// ElkEnabledFromEnv returns true when ELK_ENABLED=true.
func ElkEnabledFromEnv() bool {
	v := os.Getenv("ELK_ENABLED")
	return v == "true" || v == "1" || v == "yes"
}

// ── multiSlogHandler ──────────────────────────────────────────────────────────

type multiSlogHandler struct {
	handlers []slog.Handler
}

func (m *multiSlogHandler) Enabled(ctx context.Context, level slog.Level) bool {
	for _, h := range m.handlers {
		if h.Enabled(ctx, level) {
			return true
		}
	}
	return false
}

func (m *multiSlogHandler) Handle(ctx context.Context, r slog.Record) error {
	for _, h := range m.handlers {
		if h.Enabled(ctx, r.Level) {
			_ = h.Handle(ctx, r.Clone())
		}
	}
	return nil
}

func (m *multiSlogHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	hs := make([]slog.Handler, len(m.handlers))
	for i, h := range m.handlers {
		hs[i] = h.WithAttrs(attrs)
	}
	return &multiSlogHandler{handlers: hs}
}

func (m *multiSlogHandler) WithGroup(name string) slog.Handler {
	hs := make([]slog.Handler, len(m.handlers))
	for i, h := range m.handlers {
		hs[i] = h.WithGroup(name)
	}
	return &multiSlogHandler{handlers: hs}
}

// ── elkHandler ────────────────────────────────────────────────────────────────

// elkState holds the mutable, shared fields that must NOT be copied.
// elkHandler pointers created via WithAttrs all share the same *elkState so
// that a single flushWorker drains one buffer and one mutex is used.
// Previously, WithAttrs did `n := *h` which copied sync.Mutex after first use
// (go vet: "assignment copies lock value") and gave derived handlers a separate
// buf/mu, silently dropping their log records from the flush path.
type elkState struct {
	mu      sync.Mutex
	buf     []map[string]any
	stopCh  chan struct{}
	flushCh chan struct{}
}

type elkHandler struct {
	endpoint   string
	index      string
	svcName    string
	svcVersion string
	env        string
	preAttrs   []slog.Attr // pre-set attributes from WithAttrs calls

	state *elkState // shared across all derived handlers
}

func (h *elkHandler) Enabled(_ context.Context, _ slog.Level) bool { return true }

func (h *elkHandler) Handle(_ context.Context, r slog.Record) error {
	doc := map[string]any{
		"@timestamp":      r.Time.UTC().Format("2006-01-02T15:04:05.000Z"),
		"level":           r.Level.String(),
		"message":         r.Message,
		"service.name":    h.svcName,
		"service.version": h.svcVersion,
		"environment":     h.env,
	}
	// Apply pre-set attrs from WithAttrs (e.g. service-level correlation IDs).
	for _, a := range h.preAttrs {
		doc[a.Key] = a.Value.Any()
	}
	r.Attrs(func(a slog.Attr) bool {
		doc[a.Key] = a.Value.Any()
		return true
	})

	h.state.mu.Lock()
	h.state.buf = append(h.state.buf, doc)
	full := len(h.state.buf) >= 200
	h.state.mu.Unlock()

	if full {
		select {
		case h.state.flushCh <- struct{}{}:
		default:
		}
	}
	return nil
}

func (h *elkHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	merged := make([]slog.Attr, len(h.preAttrs)+len(attrs))
	copy(merged, h.preAttrs)
	copy(merged[len(h.preAttrs):], attrs)
	return &elkHandler{
		endpoint:   h.endpoint,
		index:      h.index,
		svcName:    h.svcName,
		svcVersion: h.svcVersion,
		env:        h.env,
		preAttrs:   merged,
		state:      h.state, // share the same mutex, buffer, and channels
	}
}

func (h *elkHandler) WithGroup(_ string) slog.Handler { return h }

func (h *elkHandler) flushWorker() {
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()
	for {
		select {
		case <-h.state.stopCh:
			return
		case <-ticker.C:
			h.flush()
		case <-h.state.flushCh:
			h.flush()
		}
	}
}

func (h *elkHandler) flush() {
	h.state.mu.Lock()
	if len(h.state.buf) == 0 {
		h.state.mu.Unlock()
		return
	}
	batch := h.state.buf
	h.state.buf = nil
	h.state.mu.Unlock()

	var body bytes.Buffer
	for _, doc := range batch {
		meta, _ := json.Marshal(map[string]any{"index": map[string]any{"_index": h.index}})
		data, _ := json.Marshal(doc)
		body.Write(meta)
		body.WriteByte('\n')
		body.Write(data)
		body.WriteByte('\n')
	}

	url := fmt.Sprintf("%s/_bulk", h.endpoint)
	req, err := http.NewRequestWithContext(context.Background(), http.MethodPost, url, &body)
	if err != nil {
		return
	}
	req.Header.Set("Content-Type", "application/x-ndjson")

	client := &http.Client{Timeout: 10 * time.Second}
	resp, err := client.Do(req)
	if err != nil {
		return // best-effort; don't log to avoid infinite recursion
	}
	defer resp.Body.Close()
}
