// Package grantclient makes the gateway's calls to the orchestrator carry a
// signed grant.
//
// The handlers describe who is asking the way they always have: the CLI
// handlers put the caller ("login@platform") in the outgoing x-cli-user
// metadata and the tenant in x-tenant-id. Those are plain strings any client
// could have written, so the orchestrator does not accept them as an identity.
// This interceptor, installed on the orchestrator connection, turns them into
// a short-lived signed grant (see libs/go/grants) and removes the raw headers:
// the orchestrator verifies the grant on every call and acts only inside its
// claims.
//
// The platform and datasource claims come from the request message itself, so
// a grant always describes exactly the call it travels with.
package grantclient

import (
	"context"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	"vartrack/libs/grants"
)

const (
	callerHeader = "x-cli-user"
	tenantHeader = "x-tenant-id"

	// serviceSubject names the gateway itself on calls made for no CLI user,
	// such as a forwarded webhook.
	serviceSubject = "gateway"
)

type hasDatasource interface{ GetDatasource() string }
type hasPlatform interface{ GetPlatform() string }

// UnaryInterceptor signs every unary call with a grant minted by m.
func UnaryInterceptor(m *grants.Minter) grpc.UnaryClientInterceptor {
	return func(ctx context.Context, method string, req, reply any, cc *grpc.ClientConn,
		invoker grpc.UnaryInvoker, opts ...grpc.CallOption) error {

		claims, ctx := claimsFor(ctx, req)
		token, err := m.Mint(claims)
		if err != nil {
			return status.Errorf(codes.Internal, "sign the call to the orchestrator: %v", err)
		}
		return invoker(grants.WithBearer(ctx, token), method, req, reply, cc, opts...)
	}
}

// claimsFor reads the caller and tenant the handlers set (the tenant the permission engine
// checked), takes platform and datasource from the request, and returns the context without
// the raw caller headers. The request's own tenant_id is never copied into the grant: a name a
// request carries is a request, and a grant carries only what was decided.
func claimsFor(ctx context.Context, req any) (grants.Claims, context.Context) {
	claims := grants.Claims{Audience: grants.AudienceSync, Subject: serviceSubject}

	md, _ := metadata.FromOutgoingContext(ctx)
	md = md.Copy()
	if v := md.Get(callerHeader); len(v) > 0 && v[0] != "" {
		claims.Subject = v[0]
	}
	if v := md.Get(tenantHeader); len(v) > 0 {
		claims.Tenant = v[0]
	}
	md.Delete(callerHeader)
	md.Delete(tenantHeader)

	if r, ok := req.(hasPlatform); ok {
		claims.Platform = r.GetPlatform()
	}
	if r, ok := req.(hasDatasource); ok {
		claims.Datasource = r.GetDatasource()
	}
	return claims, metadata.NewOutgoingContext(ctx, md)
}
