package watcher

import (
	"testing"
)

// ── FingerprintRecords ────────────────────────────────────────────────────────

func TestFingerprintRecords_deterministic(t *testing.T) {
	a := FingerprintRecords(map[string]string{"x": "1", "y": "2", "z": "3"})
	b := FingerprintRecords(map[string]string{"z": "3", "x": "1", "y": "2"})
	if a != b {
		t.Errorf("expected same hash regardless of map order: %s != %s", a, b)
	}
}

func TestFingerprintRecords_differentValues(t *testing.T) {
	a := FingerprintRecords(map[string]string{"k": "v1"})
	b := FingerprintRecords(map[string]string{"k": "v2"})
	if a == b {
		t.Error("different values must produce different fingerprints")
	}
}

func TestFingerprintRecords_differentKeys(t *testing.T) {
	a := FingerprintRecords(map[string]string{"k1": "v"})
	b := FingerprintRecords(map[string]string{"k2": "v"})
	if a == b {
		t.Error("different keys must produce different fingerprints")
	}
}

func TestFingerprintRecords_empty(t *testing.T) {
	fp := FingerprintRecords(map[string]string{})
	if len(fp) != 64 {
		t.Errorf("expected 64-char sha256 hex, got len=%d", len(fp))
	}
}

func TestFingerprintRecords_singleEntry(t *testing.T) {
	fp := FingerprintRecords(map[string]string{"key": "val"})
	if len(fp) != 64 {
		t.Errorf("expected 64-char sha256 hex, got len=%d", len(fp))
	}
}

// ── StateStore ────────────────────────────────────────────────────────────────

func TestStateStore_saveAndLoad(t *testing.T) {
	store, err := NewStateStore(t.TempDir())
	if err != nil {
		t.Fatalf("NewStateStore: %v", err)
	}

	const want = "abc123fingerprint"
	if err := store.Save("mykey", want); err != nil {
		t.Fatalf("Save: %v", err)
	}

	got := store.Load("mykey")
	if got != want {
		t.Errorf("Load: got %q, want %q", got, want)
	}
}

func TestStateStore_loadMissing(t *testing.T) {
	store, err := NewStateStore(t.TempDir())
	if err != nil {
		t.Fatalf("NewStateStore: %v", err)
	}
	if got := store.Load("nonexistent"); got != "" {
		t.Errorf("Load missing key: expected empty string, got %q", got)
	}
}

func TestStateStore_overwrite(t *testing.T) {
	store, err := NewStateStore(t.TempDir())
	if err != nil {
		t.Fatalf("NewStateStore: %v", err)
	}
	_ = store.Save("k", "first")
	_ = store.Save("k", "second")
	if got := store.Load("k"); got != "second" {
		t.Errorf("expected overwritten value %q, got %q", "second", got)
	}
}

func TestStateStore_sanitizesKey(t *testing.T) {
	store, err := NewStateStore(t.TempDir())
	if err != nil {
		t.Fatalf("NewStateStore: %v", err)
	}
	// Keys with special characters must not panic or error.
	key := "mongo/acme-payments:prod"
	if err := store.Save(key, "fp"); err != nil {
		t.Fatalf("Save with special-char key: %v", err)
	}
	if got := store.Load(key); got != "fp" {
		t.Errorf("Load special-char key: got %q, want %q", got, "fp")
	}
}
