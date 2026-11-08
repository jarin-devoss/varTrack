package main

import (
	"context"
	"fmt"
	"log/slog"

	"gateway-service/internal/config"
	"gateway-service/internal/degraded"
	"gateway-service/internal/middlewares"
	"gateway-service/internal/models"
)

// nonceStoreFor is where the gateway keeps webhook delivery ids, so that a delivery replayed to another
// replica is refused.
//
//   - config.cue names a Redis datasource (gateway_nonce_datasource): the gateway connects to it, and does
//     not start when it cannot. A store that was asked for and is not there is not replaced by one that
//     remembers nothing the other replicas know.
//   - none is named: production does not start (it may run more than one replica, and each would keep its
//     own memory); elsewhere the gateway keeps them in this process, which only the operator who named the
//     "nonce_store" degraded mode (VARTRACK_ALLOW_DEGRADED) accepts, and readiness lists while it is on.
//
// It returns a nil store for the in-process mode (the router's default).
func nonceStoreFor(ctx context.Context, bundle *models.Bundle, production bool) (middlewares.NonceStore, error) {
	name, opts, err := config.NonceRedisOptions(ctx, bundle)
	if err != nil {
		return degradedOrError(production, err)
	}
	if name == "" {
		return degradedOrError(production, fmt.Errorf(
			"config.cue names no gateway_nonce_datasource: webhook replay protection needs a Redis datasource "+
				"shared by every gateway replica"))
	}
	store, err := middlewares.NewRedisNonceStoreWithOptions(opts)
	if err != nil {
		return degradedOrError(production, fmt.Errorf("the nonce store %q is unreachable: %w", name, err))
	}
	slog.Info("gateway: distributed Redis nonce store enabled", "datasource", name)
	return store, nil
}

// degradedOrError is what replaces a nonce store that cannot be had: nothing, unless the operator allowed
// the in-process one by name and this environment may use it.
func degradedOrError(production bool, cause error) (middlewares.NonceStore, error) {
	allowed, err := degraded.Allowed("nonce_store", production)
	if err != nil {
		return nil, err
	}
	if !allowed {
		hint := "name the mode in VARTRACK_ALLOW_DEGRADED=nonce_store to accept an in-process store (never in production)"
		if production {
			hint = "production does not run without it"
		}
		return nil, fmt.Errorf("webhook replay protection: %w (%s)", cause, hint)
	}
	degraded.On("nonce_store", cause.Error())
	return nil, nil
}
