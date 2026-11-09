package healer

import (
	"context"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"vartrack/libs/grants"
)

// watcherSubject names the watcher in the grants it signs.
const watcherSubject = "watcher"

// signCalls returns a client interceptor that gives every call to the
// orchestrator a short-lived "watch" grant. The orchestrator checks it on every
// method of its Watcher service and refuses a call that has none, so the watcher
// is the one thing that can drive a self-heal, not anything that can reach the port.
func signCalls(m *grants.Minter) grpc.UnaryClientInterceptor {
	return func(ctx context.Context, method string, req, reply any, cc *grpc.ClientConn,
		invoker grpc.UnaryInvoker, opts ...grpc.CallOption) error {

		token, err := m.Mint(grants.Claims{Audience: grants.AudienceWatch, Subject: watcherSubject})
		if err != nil {
			return status.Errorf(codes.Internal, "sign the call to the orchestrator: %v", err)
		}
		return invoker(grants.WithBearer(ctx, token), method, req, reply, cc, opts...)
	}
}
