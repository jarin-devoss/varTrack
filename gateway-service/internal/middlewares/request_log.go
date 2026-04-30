package middlewares

import (
	"log/slog"
	"net/http"
	"time"
)

// RequestLog logs completed HTTP requests with timing and context IDs.
//
// Log levels:
//   - 5xx → Error
//   - 4xx → Warn
//   - 2xx/3xx → suppressed (reduces noise in normal operation)
func RequestLog(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		start := time.Now()
		sw := &statusWriter{ResponseWriter: w, status: http.StatusOK}
		next.ServeHTTP(sw, r)

		dur := time.Since(start)
		attrs := []any{
			"method", r.Method,
			"path", r.URL.Path,
			"status", sw.status,
			"duration", dur.String(),
			"correlation_id", GetCorrelationID(r.Context()),
			"request_id", GetRequestID(r.Context()),
		}

		switch {
		case sw.status >= 500:
			slog.Error("request completed", attrs...)
		case sw.status >= 400:
			slog.Warn("request completed", attrs...)
		default:
			// 2xx / 3xx — logged at Debug so normal traffic is visible
			// when log level is debug, without adding noise in production.
			slog.Debug("request completed", attrs...)
		}
	})
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (w *statusWriter) WriteHeader(code int) {
	w.status = code
	w.ResponseWriter.WriteHeader(code)
}
