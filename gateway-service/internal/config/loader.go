package config

import (
	"fmt"

	"gateway-service/internal/models"

	"vartrack/libs/bundle"

	// Register platform drivers.
	_ "gateway-service/internal/models/platforms"
	// Register secret manager drivers.
	_ "gateway-service/internal/models/secretmanagers"
)

// NewBundle loads config.cue at cuePath. The gateway reads it strictly: a
// field the proto does not declare stops it at startup, so a typo is found
// when the service starts, not when a webhook needs the setting.
func NewBundle(cuePath string) (*models.Bundle, error) {
	loaded, err := bundle.Load(cuePath, bundle.Options{Strict: true})
	if err != nil {
		return nil, fmt.Errorf("failed to load bundle from CUE: %w", err)
	}

	return models.NewBundle(loaded), nil
}
