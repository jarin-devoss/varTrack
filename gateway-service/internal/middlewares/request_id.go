package middlewares

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"net/http"
	"strconv"
	"sync/atomic"
)

// contextKey is an unexported type for context keys to avoid collisions.
type contextKey string

const (
	// HeaderRequestID is the header added to each response.
	HeaderRequestID = "X-Request-ID"

	requestIDKey contextKey = "requestID"
)

// reqIDPrefix is a 6-byte random prefix generated once at startup so request
// IDs are unique across process restarts.  reqIDSeq provides per-process
// monotonic uniqueness without any lock on the hot path.
var (
	reqIDPrefix string
	reqIDSeq    atomic.Uint64
)

func init() {
	b := make([]byte, 6)
	if _, err := rand.Read(b); err != nil {
		// Fallback: all-zeros prefix — still unique within the process via counter.
		b = make([]byte, 6)
	}
	reqIDPrefix = hex.EncodeToString(b)
}

func newRequestID() string {
	seq := reqIDSeq.Add(1)
	return reqIDPrefix + "-" + strconv.FormatUint(seq, 36)
}

// RequestID generates a unique X-Request-ID for each HTTP transaction.
func RequestID(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		id := newRequestID()
		ctx := context.WithValue(r.Context(), requestIDKey, id)
		w.Header().Set(HeaderRequestID, id)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

// GetRequestID retrieves the request ID from the context.
func GetRequestID(ctx context.Context) string {
	if id, ok := ctx.Value(requestIDKey).(string); ok {
		return id
	}
	return ""
}
