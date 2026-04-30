// Package driver provides generic, thread-safe registries and factories
// for pluggable driver implementations (e.g. platforms, secret managers).
package driver

import (
	"context"
	"fmt"
	"sync"
)

// Registry is a generic, thread-safe registry for drivers that follow
// the Open pattern (e.g. platforms, secret managers).
type Registry[D any, C any] struct {
	mu       sync.RWMutex
	drivers  map[string]func() D
	opener   func(driver D, ctx context.Context, config C) (D, error)
	typeName string
}

// NewRegistry creates a Registry. The opener callback is called to
// initialise a freshly created driver with the provided config.
func NewRegistry[D any, C any](
	typeName string,
	opener func(driver D, ctx context.Context, config C) (D, error),
) *Registry[D, C] {
	return &Registry[D, C]{
		drivers:  make(map[string]func() D),
		opener:   opener,
		typeName: typeName,
	}
}

// Register adds a driver constructor under the given name.
// It panics if f is nil or if a driver is registered twice under the same name.
func (r *Registry[D, C]) Register(name string, f func() D) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if f == nil {
		panic(fmt.Sprintf("%s: Register driver is nil", r.typeName))
	}
	if _, dup := r.drivers[name]; dup {
		panic(fmt.Sprintf("%s: Register called twice for driver %s", r.typeName, name))
	}
	r.drivers[name] = f
}

// Open creates and opens a driver by name, using the registry's opener callback.
func (r *Registry[D, C]) Open(ctx context.Context, name string, config C) (D, error) {
	r.mu.RLock()
	f, ok := r.drivers[name]
	r.mu.RUnlock()

	var zero D
	if !ok {
		return zero, fmt.Errorf("%s: unknown driver %q", r.typeName, name)
	}

	driver := f()
	return r.opener(driver, ctx, config)
}
