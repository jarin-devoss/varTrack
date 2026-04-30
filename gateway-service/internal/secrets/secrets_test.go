package secrets_test

import (
	"context"
	"errors"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"gateway-service/internal/secrets"

	pb_utils "gateway-service/internal/gen/proto/go/vartrack/v1/utils"
)

// stubFetcher is a minimal Fetcher for tests.
type stubFetcher struct {
	values map[string]string
	calls  atomic.Int64
}

func (s *stubFetcher) GetSecret(_ context.Context, path, key string) (string, error) {
	s.calls.Add(1)
	v, ok := s.values[path+"/"+key]
	if !ok {
		return "", errors.New("not found")
	}
	return v, nil
}

func newResolver(fetcher *stubFetcher) *secrets.RefResolver {
	return secrets.NewRefResolver(
		func(_ context.Context, _ string) (secrets.Fetcher, error) {
			return fetcher, nil
		},
	)
}

func TestRefResolver_NilRef(t *testing.T) {
	r := newResolver(&stubFetcher{})
	v, err := r.Resolve(context.Background(), nil, "mgr")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if v != "" {
		t.Errorf("got %q, want empty string", v)
	}
}

func TestRefResolver_InlineValue(t *testing.T) {
	r := newResolver(&stubFetcher{})
	ref := &pb_utils.SecretRef{
		Source: &pb_utils.SecretRef_Value{Value: "hunter2"},
	}
	v, err := r.Resolve(context.Background(), ref, "mgr")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if v != "hunter2" {
		t.Errorf("got %q, want %q", v, "hunter2")
	}
}

func TestRefResolver_ExternalRef(t *testing.T) {
	fetcher := &stubFetcher{
		values: map[string]string{"secret/data/app/token": "s3cret"},
	}
	r := newResolver(fetcher)

	ref := &pb_utils.SecretRef{
		Source: &pb_utils.SecretRef_Ref{
			Ref: &pb_utils.ExternalRef{
				Path: "secret/data/app",
				Key:  "token",
			},
		},
	}
	v, err := r.Resolve(context.Background(), ref, "vault")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if v != "s3cret" {
		t.Errorf("got %q, want %q", v, "s3cret")
	}
}

func TestRefResolver_NoManagerName(t *testing.T) {
	r := newResolver(&stubFetcher{})
	ref := &pb_utils.SecretRef{
		Source: &pb_utils.SecretRef_Ref{
			Ref: &pb_utils.ExternalRef{Path: "p", Key: "k"},
		},
	}
	_, err := r.Resolve(context.Background(), ref, "")
	if err == nil {
		t.Fatal("expected error when manager name is empty")
	}
}

// slowFetcher adds artificial delay so singleflight has time to coalesce.
type slowFetcher struct {
	values map[string]string
	calls  atomic.Int64
	delay  time.Duration
}

func (s *slowFetcher) GetSecret(_ context.Context, path, key string) (string, error) {
	s.calls.Add(1)
	time.Sleep(s.delay)
	v, ok := s.values[path+"/"+key]
	if !ok {
		return "", errors.New("not found")
	}
	return v, nil
}

func TestRefResolver_Singleflight(t *testing.T) {
	fetcher := &slowFetcher{
		values: map[string]string{"path/key": "value"},
		delay:  50 * time.Millisecond,
	}
	r := secrets.NewRefResolver(
		func(_ context.Context, _ string) (secrets.Fetcher, error) {
			return fetcher, nil
		},
	)

	ref := &pb_utils.SecretRef{
		Source: &pb_utils.SecretRef_Ref{
			Ref: &pb_utils.ExternalRef{Path: "path", Key: "key"},
		},
	}

	const n = 10

	// Barrier: all goroutines wait until ready is closed.
	ready := make(chan struct{})
	var wg sync.WaitGroup
	for i := 0; i < n; i++ {
		wg.Add(1)
		go func() {
			defer wg.Done()
			<-ready
			_, _ = r.Resolve(context.Background(), ref, "mgr")
		}()
	}
	close(ready) // release all goroutines simultaneously
	wg.Wait()

	calls := fetcher.calls.Load()
	if calls > 3 {
		t.Errorf("expected singleflight coalescing, got %d calls to GetSecret (want ≤3)", calls)
	}
}

// --- CachingRefResolver tests ---

func TestCachingRefResolver_CachesExternalRefs(t *testing.T) {
	fetcher := &stubFetcher{
		values: map[string]string{"p/k": "cached-val"},
	}
	inner := newResolver(fetcher)
	c := secrets.NewCachingRefResolver(inner, secrets.CacheConfig{
		TTL:             1 * time.Minute,
		CleanupInterval: 1 * time.Hour,
	})
	defer c.Close()

	ref := &pb_utils.SecretRef{
		Source: &pb_utils.SecretRef_Ref{
			Ref: &pb_utils.ExternalRef{Path: "p", Key: "k"},
		},
	}

	// First resolve — cache miss.
	v1, err := c.Resolve(context.Background(), ref, "mgr")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if v1 != "cached-val" {
		t.Errorf("got %q, want %q", v1, "cached-val")
	}

	firstCalls := fetcher.calls.Load()

	// Second resolve — should come from cache (no additional fetcher calls).
	v2, err := c.Resolve(context.Background(), ref, "mgr")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if v2 != "cached-val" {
		t.Errorf("got %q, want %q", v2, "cached-val")
	}

	if fetcher.calls.Load() != firstCalls {
		t.Errorf("expected cached response, but fetcher was called again")
	}
}

func TestCachingRefResolver_InlinePassthrough(t *testing.T) {
	fetcher := &stubFetcher{}
	inner := newResolver(fetcher)
	c := secrets.NewCachingRefResolver(inner, secrets.DefaultCacheConfig())
	defer c.Close()

	ref := &pb_utils.SecretRef{
		Source: &pb_utils.SecretRef_Value{Value: "inline-val"},
	}

	v, err := c.Resolve(context.Background(), ref, "mgr")
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if v != "inline-val" {
		t.Errorf("got %q, want %q", v, "inline-val")
	}
}

func TestCachingRefResolver_InvalidateAll(t *testing.T) {
	fetcher := &stubFetcher{
		values: map[string]string{"p/k": "val"},
	}
	inner := newResolver(fetcher)
	c := secrets.NewCachingRefResolver(inner, secrets.CacheConfig{
		TTL:             1 * time.Minute,
		CleanupInterval: 1 * time.Hour,
	})
	defer c.Close()

	ref := &pb_utils.SecretRef{
		Source: &pb_utils.SecretRef_Ref{
			Ref: &pb_utils.ExternalRef{Path: "p", Key: "k"},
		},
	}

	// Populate cache.
	_, _ = c.Resolve(context.Background(), ref, "mgr")
	if c.Len() != 1 {
		t.Fatalf("expected 1 cached entry, got %d", c.Len())
	}

	c.InvalidateAll()
	if c.Len() != 0 {
		t.Errorf("expected 0 entries after invalidation, got %d", c.Len())
	}
}
