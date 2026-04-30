package monitoring

import (
	"sync"
)

// MetricProvider defines the behavior for any metrics backend.
type MetricProvider interface {
	Inc(name string, labels map[string]string)
	Observe(name string, value float64, labels map[string]string)
	Set(name string, value float64, labels map[string]string)
}

// nopProvider handles cases where no provider is registered.
type nopProvider struct{}

func (n *nopProvider) Inc(name string, labels map[string]string)                    {}
func (n *nopProvider) Observe(name string, value float64, labels map[string]string) {}
func (n *nopProvider) Set(name string, value float64, labels map[string]string)     {}

var (
	globalProvider MetricProvider = &nopProvider{}
	mu             sync.RWMutex
)

func RegisterProvider(p MetricProvider) {
	mu.Lock()
	defer mu.Unlock()
	globalProvider = p
}

// Inc increments a counter.
// Usage: monitoring.Inc("requests_total", "method", "GET")
func Inc(name string, labelPairs ...string) {
	labels := pairsToMap(labelPairs)
	mu.RLock()
	defer mu.RUnlock()
	globalProvider.Inc(name, labels)
}

// Set records a specific value (Gauge).
func Set(name string, value float64, labelPairs ...string) {
	labels := pairsToMap(labelPairs)
	mu.RLock()
	defer mu.RUnlock()
	globalProvider.Set(name, value, labels)
}

// Observe records a histogram value (Latency).
func Observe(name string, value float64, labelPairs ...string) {
	labels := pairsToMap(labelPairs)
	mu.RLock()
	defer mu.RUnlock()
	globalProvider.Observe(name, value, labels)
}

func pairsToMap(pairs []string) map[string]string {
	m := make(map[string]string, len(pairs)/2)
	for i := 0; i < len(pairs)-1; i += 2 {
		m[pairs[i]] = pairs[i+1]
	}
	return m
}
