package models

import (
	"context"
	"fmt"

	"gateway-service/internal/driver"

	pb_models "gateway-service/internal/gen/proto/go/vartrack/v1/models"
)

// SecretManager is the interface all secret manager drivers must implement.
type SecretManager interface {
	// Open initialises the driver (authenticate, build client, etc.).
	Open(ctx context.Context, config *pb_models.SecretManager) (SecretManager, error)

	// GetSecret fetches a secret value by path and key.
	GetSecret(ctx context.Context, path, key string) (string, error)

	// Ping verifies connectivity to the backend (e.g. Vault sys/health).
	// Must be safe to call concurrently and must not mutate state.
	Ping(ctx context.Context) error

	// Close releases resources held by the driver.
	Close(ctx context.Context) error
}

// SecretManagerRegistry is the global registry for secret manager drivers.
var SecretManagerRegistry = driver.NewRegistry[SecretManager, *pb_models.SecretManager](
	"secret_manager",
	func(d SecretManager, ctx context.Context, config *pb_models.SecretManager) (SecretManager, error) {
		return d.Open(ctx, config)
	},
)

// SecretManagerFactory creates secret manager instances via the registry.
var SecretManagerFactory = driver.NewFactory(
	SecretManagerRegistry,
	func(c *pb_models.SecretManager) string {
		if c == nil {
			return ""
		}
		return SecretManagerName(c)
	},
	func(c *pb_models.SecretManager) error {
		if c == nil {
			return fmt.Errorf("secret manager config cannot be nil")
		}
		return nil
	},
	"secret_manager",
)

// SecretManagerName returns the resolved name for a secret manager.
// If a tag is set, the name is "{type}-{tag}" (e.g. "vault-prod").
func SecretManagerName(sm *pb_models.SecretManager) string {
	switch config := sm.Config.(type) {
	case *pb_models.SecretManager_Vault:
		return driver.ResolveTagName("vault", config.Vault.GetTag())
	default:
		return ""
	}
}
