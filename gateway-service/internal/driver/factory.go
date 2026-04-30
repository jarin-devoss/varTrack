package driver

import (
	"context"
	"fmt"
)

// Factory wraps a Registry with validation and name resolution.
type Factory[D any, C any] struct {
	registry     *Registry[D, C]
	nameFunc     func(C) string
	validateFunc func(C) error
	typeName     string
}

// NewFactory creates a Factory backed by the given registry.
func NewFactory[D any, C any](
	registry *Registry[D, C],
	nameFunc func(C) string,
	validateFunc func(C) error,
	typeName string,
) *Factory[D, C] {
	return &Factory[D, C]{
		registry:     registry,
		nameFunc:     nameFunc,
		validateFunc: validateFunc,
		typeName:     typeName,
	}
}

// Get validates the config, resolves the driver name, and opens the driver.
func (f *Factory[D, C]) Get(ctx context.Context, config C) (D, error) {
	var zero D

	if f.validateFunc != nil {
		if err := f.validateFunc(config); err != nil {
			return zero, err
		}
	}

	name := f.nameFunc(config)
	if name == "" {
		return zero, fmt.Errorf("%s: name must be specified", f.typeName)
	}

	return f.registry.Open(ctx, name, config)
}
