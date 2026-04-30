// Package secrets provides secret reference resolution with singleflight
// deduplication and optional TTL caching.
package secrets

import (
	"context"
	"fmt"
	"time"

	pb_utils "gateway-service/internal/gen/proto/go/vartrack/v1/utils"

	"golang.org/x/sync/singleflight"
)

// Fetcher is the interface for reaching the secret backend.
type Fetcher interface {
	GetSecret(ctx context.Context, path, key string) (string, error)
}

// Resolver resolves SecretRef values to their plain-text secret strings.
type Resolver interface {
	Resolve(ctx context.Context, ref *pb_utils.SecretRef, managerName string) (string, error)
}

// RefResolver resolves SecretRef values — either returning the inline
// value or fetching from the secret manager via singleflight deduplication.
//
// A singleflight.Group ensures that concurrent requests for the same
// external secret result in only one network call.
type RefResolver struct {
	getManager func(ctx context.Context, name string) (Fetcher, error)
	sfg        singleflight.Group
}

// NewRefResolver creates a resolver. The getManager callback is
// called to obtain a Fetcher for a given manager name.
func NewRefResolver(getManager func(ctx context.Context, name string) (Fetcher, error)) *RefResolver {
	return &RefResolver{getManager: getManager}
}

// Resolve returns the plain-text value for a SecretRef.
//   - nil ref → empty string
//   - inline value → returned directly (zero overhead)
//   - external ref → fetched via singleflight deduplication
//
// The provided context is threaded into the secret manager call so that
// the caller's deadline (e.g. webhook handler's 10s timeout) is honoured.
func (r *RefResolver) Resolve(ctx context.Context, ref *pb_utils.SecretRef, managerName string) (string, error) {
	if ref == nil {
		return "", nil
	}

	switch source := ref.Source.(type) {
	case *pb_utils.SecretRef_Value:
		return source.Value, nil

	case *pb_utils.SecretRef_Ref:
		extRef := source.Ref
		if extRef.Path == "" || extRef.Key == "" {
			return "", fmt.Errorf("external secret ref: path and key are required")
		}

		if managerName == "" {
			return "", fmt.Errorf(
				"secret ref (path=%q, key=%q) requires a secret_manager but none is configured in the rule",
				extRef.Path, extRef.Key,
			)
		}

		// Deduplication key using \x00 separator (cannot appear in valid paths).
		sfKey := managerName + "\x00" + extRef.Path + "\x00" + extRef.Key

		// context.WithoutCancel prevents one caller's cancellation from
		// aborting the shared request on behalf of other waiters.
		// However, it also removes deadlines, so we explicitly add a 10s timeout.
		result, err, _ := r.sfg.Do(sfKey, func() (interface{}, error) {
			fetchCtx, cancel := context.WithTimeout(context.WithoutCancel(ctx), 10*time.Second)
			defer cancel()

			sm, err := r.getManager(fetchCtx, managerName)
			if err != nil {
				return "", fmt.Errorf("failed to get secret manager %q: %w", managerName, err)
			}

			value, err := sm.GetSecret(fetchCtx, extRef.Path, extRef.Key)
			if err != nil {
				return "", fmt.Errorf(
					"failed to resolve secret (manager=%s, path=%s, key=%s): %w",
					managerName, extRef.Path, extRef.Key, err,
				)
			}
			return value, nil
		})
		if err != nil {
			return "", err
		}

		resolved := result.(string)
		RegisterSecret(resolved)
		return resolved, nil

	default:
		return "", fmt.Errorf("secret ref has no source set")
	}
}
