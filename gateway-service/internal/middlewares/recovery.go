package middlewares

import (
	"log/slog"
	"net/http"
	"runtime/debug"
)

// Recovery returns middleware that catches panics in downstream handlers,
// logs them with stack traces, and returns a 500 error.
func Recovery() func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			defer func() {
				if err := recover(); err != nil {
					stack := debug.Stack()
					slog.Error("panic recovered",
						"error", err,
						"method", r.Method,
						"path", r.URL.Path,
						"stack", string(stack),
						"correlation_id", GetCorrelationID(r.Context()),
						"request_id", GetRequestID(r.Context()),
					)

					// Only write 500 if headers have not been sent.
					rw, ok := w.(*recoverResponseWriter)
					if ok && rw.written {
						return
					}
					http.Error(w, http.StatusText(http.StatusInternalServerError),
						http.StatusInternalServerError)
				}
			}()

			rw := &recoverResponseWriter{ResponseWriter: w}
			next.ServeHTTP(rw, r)
		})
	}
}

// recoverResponseWriter tracks whether headers have already been sent
// to avoid writing headers after a panic when the response is committed.
type recoverResponseWriter struct {
	http.ResponseWriter
	written bool
}

func (w *recoverResponseWriter) WriteHeader(code int) {
	w.written = true
	w.ResponseWriter.WriteHeader(code)
}

func (w *recoverResponseWriter) Write(b []byte) (int, error) {
	w.written = true
	return w.ResponseWriter.Write(b)
}

// Flush implements http.Flusher by delegating to the underlying ResponseWriter.
func (w *recoverResponseWriter) Flush() {
	if f, ok := w.ResponseWriter.(http.Flusher); ok {
		f.Flush()
	}
}
