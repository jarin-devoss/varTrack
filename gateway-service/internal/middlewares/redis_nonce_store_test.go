package middlewares

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/redis/go-redis/v9"
)

// A store whose Redis is not there answers with an error, never "this nonce is new".
func TestRedisNonceStoreThatCannotBeAskedIsAnErrorNotAnAllow(t *testing.T) {
	store := &RedisNonceStore{client: redis.NewClient(&redis.Options{
		Addr: "127.0.0.1:1", DialTimeout: 100 * time.Millisecond, MaxRetries: -1,
	})}
	defer store.Close()

	fresh, err := store.CheckAndSet("delivery-1", time.Minute)
	if fresh || !errors.Is(err, ErrNonceStoreUnavailable) {
		t.Fatalf("CheckAndSet = %v, %v; want false and ErrNonceStoreUnavailable", fresh, err)
	}
	if err := store.Ping(context.Background()); err == nil {
		t.Fatal("Ping must report that Redis does not answer")
	}
}

func TestConnectingToARedisThatIsNotThereIsAnErrorNotAStore(t *testing.T) {
	opts := &redis.Options{Addr: "127.0.0.1:1", DialTimeout: 100 * time.Millisecond, MaxRetries: -1}
	if store, err := NewRedisNonceStoreWithOptions(opts); err == nil || store != nil {
		t.Fatalf("got %v, %v; want an error and no store", store, err)
	}
}

func TestAReplayProtectorPassesTheStoresErrorOn(t *testing.T) {
	rp := NewReplayProtectorWithStore(DefaultReplayProtectionConfig(), downStore{})
	if _, err := rp.CheckNonce("n"); !errors.Is(err, ErrNonceStoreUnavailable) {
		t.Fatalf("err = %v", err)
	}
	if fresh, err := rp.CheckNonce(""); !fresh || err != nil {
		t.Fatalf("no nonce, nothing to check: %v, %v", fresh, err)
	}
}

type downStore struct{}

func (downStore) CheckAndSet(string, time.Duration) (bool, error) {
	return false, ErrNonceStoreUnavailable
}
