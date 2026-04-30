package monitoring_test

import (
	"context"
	"testing"
	"time"

	"gateway-service/internal/monitoring"

	"google.golang.org/grpc/connectivity"
)

// fakeConn satisfies monitoring.ConnStateChecker.
type fakeConn struct {
	state connectivity.State
}

func (f *fakeConn) GetState() connectivity.State { return f.state }

func TestStartConnStatePoller_ExitsOnContextCancel(t *testing.T) {
	m := monitoring.DefaultMetrics()
	conn := &fakeConn{state: connectivity.Ready}

	ctx, cancel := context.WithCancel(context.Background())

	// Start with a very short interval so we get at least one tick.
	monitoring.StartConnStatePoller(ctx, conn, m, 20*time.Millisecond)

	// Let it tick at least once.
	time.Sleep(50 * time.Millisecond)

	// Cancel should cause the goroutine to exit cleanly (no deadlock / race).
	cancel()

	// Give the goroutine a moment to stop.
	time.Sleep(30 * time.Millisecond)
}

func TestStartConnStatePoller_AllStates(t *testing.T) {
	m := monitoring.DefaultMetrics()

	states := []connectivity.State{
		connectivity.Idle,
		connectivity.Connecting,
		connectivity.Ready,
		connectivity.TransientFailure,
		connectivity.Shutdown,
	}

	for _, s := range states {
		conn := &fakeConn{state: s}
		ctx, cancel := context.WithTimeout(context.Background(), 50*time.Millisecond)
		monitoring.StartConnStatePoller(ctx, conn, m, 10*time.Millisecond)
		<-ctx.Done()
		cancel()
	}
}
