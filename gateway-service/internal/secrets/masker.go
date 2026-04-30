package secrets

import (
	"context"
	"log/slog"
	"strings"
	"sync"
)

var globalMasker = &maskStore{values: make(map[string]struct{})}

type maskStore struct {
	mu     sync.RWMutex
	values map[string]struct{}
}

// RegisterSecret adds a value to the global redaction list so it is replaced
// with "***" in all subsequent log output produced by MaskingHandler.
func RegisterSecret(value string) {
	if value == "" {
		return
	}
	globalMasker.mu.Lock()
	globalMasker.values[value] = struct{}{}
	globalMasker.mu.Unlock()
}

func maskString(s string) string {
	globalMasker.mu.RLock()
	defer globalMasker.mu.RUnlock()
	for v := range globalMasker.values {
		s = strings.ReplaceAll(s, v, "***")
	}
	return s
}

func maskAttr(a slog.Attr) slog.Attr {
	switch a.Value.Kind() {
	case slog.KindString:
		return slog.String(a.Key, maskString(a.Value.String()))
	case slog.KindGroup:
		attrs := a.Value.Group()
		masked := make([]slog.Attr, len(attrs))
		for i, ga := range attrs {
			masked[i] = maskAttr(ga)
		}
		return slog.Group(a.Key, attrsToAny(masked)...)
	default:
		return a
	}
}

func attrsToAny(attrs []slog.Attr) []any {
	out := make([]any, len(attrs))
	for i, a := range attrs {
		out[i] = a
	}
	return out
}

// MaskingHandler wraps a slog.Handler and replaces registered secret values
// with "***" in every log record before forwarding to the inner handler.
type MaskingHandler struct {
	inner slog.Handler
}

// NewMaskingHandler returns a handler that redacts secrets registered via
// RegisterSecret from all log messages and attributes.
func NewMaskingHandler(inner slog.Handler) *MaskingHandler {
	return &MaskingHandler{inner: inner}
}

func (h *MaskingHandler) Enabled(ctx context.Context, level slog.Level) bool {
	return h.inner.Enabled(ctx, level)
}

func (h *MaskingHandler) Handle(ctx context.Context, r slog.Record) error {
	cloned := slog.NewRecord(r.Time, r.Level, maskString(r.Message), r.PC)
	r.Attrs(func(a slog.Attr) bool {
		cloned.AddAttrs(maskAttr(a))
		return true
	})
	return h.inner.Handle(ctx, cloned)
}

func (h *MaskingHandler) WithAttrs(attrs []slog.Attr) slog.Handler {
	masked := make([]slog.Attr, len(attrs))
	for i, a := range attrs {
		masked[i] = maskAttr(a)
	}
	return &MaskingHandler{inner: h.inner.WithAttrs(masked)}
}

func (h *MaskingHandler) WithGroup(name string) slog.Handler {
	return &MaskingHandler{inner: h.inner.WithGroup(name)}
}
