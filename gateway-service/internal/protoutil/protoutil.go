// Package protoutil provides lightweight helpers for converting between
// google.protobuf Well-Known Types and Go standard library types.
//
// These helpers centralise the conversion logic so individual drivers
// (vault, github, monitoring backends) don't each embed the same
// durationpb.Duration→time.Duration boilerplate.
package protoutil

import (
	"time"

	"google.golang.org/protobuf/types/known/durationpb"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// DurationOrDefault converts a *durationpb.Duration to time.Duration.
// If d is nil or zero, fallback is returned.
//
//	timeout := protoutil.DurationOrDefault(cfg.GetTimeout(), 30*time.Second)
func DurationOrDefault(d *durationpb.Duration, fallback time.Duration) time.Duration {
	if d == nil {
		return fallback
	}
	v := d.AsDuration()
	if v <= 0 {
		return fallback
	}
	return v
}

// DurationOrZero converts a *durationpb.Duration to time.Duration.
// Returns 0 (no timeout / caller-supplied deadline) when d is nil or zero.
func DurationOrZero(d *durationpb.Duration) time.Duration {
	if d == nil {
		return 0
	}
	return d.AsDuration()
}

// TimestampOrZero converts a *timestamppb.Timestamp to time.Time.
// Returns time.Time{} (zero) when ts is nil.
func TimestampOrZero(ts *timestamppb.Timestamp) time.Time {
	if ts == nil {
		return time.Time{}
	}
	return ts.AsTime()
}

// NowProto returns the current UTC time as a *timestamppb.Timestamp.
// Convenience wrapper to avoid importing timestamppb at every call site.
func NowProto() *timestamppb.Timestamp {
	return timestamppb.Now()
}

// ToProtoTimestamp converts a time.Time to *timestamppb.Timestamp.
// Returns nil for a zero time.
func ToProtoTimestamp(t time.Time) *timestamppb.Timestamp {
	if t.IsZero() {
		return nil
	}
	return timestamppb.New(t)
}

// ToProtoDuration converts a time.Duration to *durationpb.Duration.
// Returns nil for zero duration.
func ToProtoDuration(d time.Duration) *durationpb.Duration {
	if d == 0 {
		return nil
	}
	return durationpb.New(d)
}
