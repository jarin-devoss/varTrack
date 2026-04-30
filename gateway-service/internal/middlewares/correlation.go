package middlewares

import (
	"context"
	"net/http"

	"github.com/google/uuid"
)

const (
	// HeaderCorrelationID is used for distributed tracing across services.
	HeaderCorrelationID = "X-Correlation-ID"

	// correlationIDKey is the context key for the correlation ID.
	correlationIDKey contextKey = "correlationID"

	// maxCorrelationIDLen caps the accepted header value length to prevent
	// memory exhaustion from a caller sending a megabyte-sized correlation ID.
	// Standard UUIDs are 36 chars; 128 chars gives generous room for custom IDs.
	maxCorrelationIDLen = 128
)

// CorrelationID ensures every request has a correlation ID for distributed
// tracing. Existing IDs from upstream callers are preserved.
func CorrelationID(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := r.Header.Get(HeaderCorrelationID)
		if id == "" {
			id = uuid.NewString()
		} else if len(id) > maxCorrelationIDLen {
			id = id[:maxCorrelationIDLen]
		}

		ctx := context.WithValue(r.Context(), correlationIDKey, id)
		w.Header().Set(HeaderCorrelationID, id)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

// GetCorrelationID retrieves the correlation ID from the context.
func GetCorrelationID(ctx context.Context) string {
	if id, ok := ctx.Value(correlationIDKey).(string); ok {
		return id
	}
	return ""
}
