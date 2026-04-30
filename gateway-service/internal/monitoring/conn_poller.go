package monitoring

import (
	"context"
	"log/slog"
	"time"

	"google.golang.org/grpc/connectivity"
)

// ConnStateChecker is the subset of grpc.ClientConn needed by the state poller.
type ConnStateChecker interface {
	GetState() connectivity.State
}

// StartConnStatePoller starts a background goroutine that polls the gRPC
// connection state every interval and updates gw_orchestrator_connection_state.
//
// The goroutine exits cleanly when ctx is cancelled.
func StartConnStatePoller(ctx context.Context, conn ConnStateChecker, m *GatewayMetrics, interval time.Duration) {
	if interval <= 0 {
		interval = 15 * time.Second
	}

	go func() {
		ticker := time.NewTicker(interval)
		defer ticker.Stop()

		// Emit immediately, then on each tick.
		emitState := func() {
			state := conn.GetState()
			label := connStateLabel(state)
			m.SetOrchestratorConnectionState(label)
			slog.Debug("orchestrator connection state polled", "state", label)
		}

		emitState()
		for {
			select {
			case <-ctx.Done():
				slog.Info("connection state poller stopping")
				return
			case <-ticker.C:
				emitState()
			}
		}
	}()
}

func connStateLabel(s connectivity.State) string {
	switch s {
	case connectivity.Idle:
		return "idle"
	case connectivity.Connecting:
		return "connecting"
	case connectivity.Ready:
		return "ready"
	case connectivity.TransientFailure:
		return "transient_failure"
	case connectivity.Shutdown:
		return "shutdown"
	default:
		return "unknown"
	}
}
