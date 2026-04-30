package middlewares_test

import (
	"net/http/httptest"
	"testing"
	"time"

	"gateway-service/internal/middlewares"
)

func newRPConfig(window time.Duration) middlewares.ReplayProtectionConfig {
	return middlewares.ReplayProtectionConfig{
		Window:      window,
		FutureDrift: 30 * time.Second,
	}
}

func newRP(t *testing.T, window time.Duration) *middlewares.ReplayProtector {
	t.Helper()
	return middlewares.NewReplayProtector(newRPConfig(window))
}

// ─── Nonce tests ──────────────────────────────────────────────────────────────

func TestReplayProtector_CheckNonce(t *testing.T) {
	tests := []struct {
		name       string
		nonces     []string // nonces sent in order
		wantLast   bool     // expected result for the final call
	}{
		{
			name:     "first nonce is allowed",
			nonces:   []string{"uuid-abc"},
			wantLast: true,
		},
		{
			name:     "duplicate nonce is rejected",
			nonces:   []string{"uuid-dup", "uuid-dup"},
			wantLast: false,
		},
		{
			name:     "different nonces are both allowed",
			nonces:   []string{"uuid-1", "uuid-2"},
			wantLast: true,
		},
		{
			name:     "empty nonce always allowed (first call)",
			nonces:   []string{""},
			wantLast: true,
		},
		{
			name:     "empty nonce always allowed (second call — no tracking)",
			nonces:   []string{"", ""},
			wantLast: true,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			rp := newRP(t, 5*time.Minute)
			var got bool
			for _, n := range tc.nonces {
				got = rp.CheckNonce(n)
			}
			if got != tc.wantLast {
				t.Errorf("CheckNonce final result = %v, want %v", got, tc.wantLast)
			}
		})
	}
}

func TestReplayProtector_CheckNonce_ExpiresAfterWindow(t *testing.T) {
	// Nonces pruned after window — the same nonce should be admitted again.
	rp := middlewares.NewReplayProtector(middlewares.ReplayProtectionConfig{
		Window:      time.Nanosecond,
		FutureDrift: 30 * time.Second,
	})
	rp.CheckNonce("expiry-nonce")

	time.Sleep(2 * time.Millisecond) // outlive the 1 ns window

	// Trigger prune by calling CheckNonce — the expired entry is removed.
	if !rp.CheckNonce("expiry-nonce") {
		t.Error("expired nonce should be re-admitted after window expiry")
	}
}

// ─── Timestamp validation tests ───────────────────────────────────────────────

func TestReplayProtector_Validate(t *testing.T) {
	now := time.Now()

	tests := []struct {
		name      string
		eventTime time.Time
		wantErr   bool
	}{
		{
			name:      "zero time skips validation",
			eventTime: time.Time{},
			wantErr:   false,
		},
		{
			name:      "recent timestamp accepted",
			eventTime: now.Add(-30 * time.Second),
			wantErr:   false,
		},
		{
			name:      "timestamp at window boundary accepted",
			eventTime: now.Add(-4*time.Minute - 59*time.Second),
			wantErr:   false,
		},
		{
			name:      "timestamp past window rejected",
			eventTime: now.Add(-10 * time.Minute),
			wantErr:   true,
		},
		{
			name:      "slight future drift accepted (within 30s)",
			eventTime: now.Add(10 * time.Second),
			wantErr:   false,
		},
		{
			name:      "far future timestamp rejected",
			eventTime: now.Add(2 * time.Minute),
			wantErr:   true,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			rp := newRP(t, 5*time.Minute)
			err := rp.Validate(tc.eventTime)
			if (err != nil) != tc.wantErr {
				t.Errorf("Validate() error = %v, wantErr = %v", err, tc.wantErr)
			}
		})
	}
}

// ─── Timestamp extractor tests ────────────────────────────────────────────────

func TestExtractGitHubTimestamp(t *testing.T) {
	// GitHub has no timestamp header — always returns zero time.
	req := httptest.NewRequest("POST", "/", nil)
	ts, err := middlewares.ExtractGitHubTimestamp(req)
	if err != nil {
		t.Errorf("unexpected error: %v", err)
	}
	if !ts.IsZero() {
		t.Errorf("expected zero time, got %v", ts)
	}
}

func TestExtractSlackTimestamp(t *testing.T) {
	tests := []struct {
		name      string
		header    string
		wantUnix  int64
		wantZero  bool
		wantErr   bool
	}{
		{
			name:     "missing header returns zero",
			header:   "",
			wantZero: true,
		},
		{
			name:     "valid unix timestamp",
			header:   "1609459200",
			wantUnix: 1609459200,
		},
		{
			name:    "non-numeric header returns error",
			header:  "not-a-number",
			wantErr: true,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest("POST", "/", nil)
			if tc.header != "" {
				req.Header.Set("X-Slack-Request-Timestamp", tc.header)
			}

			ts, err := middlewares.ExtractSlackTimestamp(req)

			if (err != nil) != tc.wantErr {
				t.Fatalf("error = %v, wantErr = %v", err, tc.wantErr)
			}
			if tc.wantZero && !ts.IsZero() {
				t.Errorf("expected zero time, got %v", ts)
			}
			if tc.wantUnix != 0 && ts.Unix() != tc.wantUnix {
				t.Errorf("unix = %d, want %d", ts.Unix(), tc.wantUnix)
			}
		})
	}
}

func TestExtractStripeTimestamp(t *testing.T) {
	tests := []struct {
		name      string
		header    string
		wantUnix  int64
		wantZero  bool
		wantErr   bool
	}{
		{
			name:     "missing header returns zero",
			header:   "",
			wantZero: true,
		},
		{
			name:     "valid t= field parsed",
			header:   "t=1609459200,v1=abc123",
			wantUnix: 1609459200,
		},
		{
			name:     "multiple fields: t= first",
			header:   "t=1609459200,v1=sig1,v2=sig2",
			wantUnix: 1609459200,
		},
		{
			name:     "no t= field returns zero",
			header:   "v1=abc123signature",
			wantZero: true,
		},
		{
			name:    "invalid t= value returns error",
			header:  "t=not-a-number,v1=abc",
			wantErr: true,
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			req := httptest.NewRequest("POST", "/", nil)
			if tc.header != "" {
				req.Header.Set("Stripe-Signature", tc.header)
			}

			ts, err := middlewares.ExtractStripeTimestamp(req)

			if (err != nil) != tc.wantErr {
				t.Fatalf("error = %v, wantErr = %v", err, tc.wantErr)
			}
			if tc.wantZero && !ts.IsZero() {
				t.Errorf("expected zero time, got %v", ts)
			}
			if tc.wantUnix != 0 && ts.Unix() != tc.wantUnix {
				t.Errorf("unix = %d, want %d", ts.Unix(), tc.wantUnix)
			}
		})
	}
}
