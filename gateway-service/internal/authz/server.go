package authz

// server.go — the engine as the vartrack.v1.Authz gRPC service.
//
// The orchestrator's web UI asks it who may see and do what; the answer is the one
// the CLI gets, from the same Engine. Every call must carry a signed grant for the
// "authz" audience (the orchestrator mints it), checked here before any handler
// runs. The platform tokens travel as request fields, over TLS in production, and
// are never logged: this file logs no request.

import (
	"context"
	"errors"
	"log/slog"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"

	"gateway-service/internal/models"
	pb "vartrack/libs/gen/vartrack/v1/services"
	"vartrack/libs/grants"
)

// Server serves the engine over gRPC.
type Server struct {
	pb.UnimplementedAuthzServer
	engine *Engine
	secret string // verifies "authz" grants; empty runs open (development only)
}

// NewServer serves engine. A call must carry a grant signed with secret; an empty
// secret accepts every call and is for development against an orchestrator that
// runs without internal authentication.
func NewServer(engine *Engine, secret string) *Server {
	return &Server{engine: engine, secret: secret}
}

// Register adds the service to gs, behind the grant check.
func (s *Server) Register(gs *grpc.Server) { pb.RegisterAuthzServer(gs, s) }

// ServerOption is the grpc server option that puts the grant check in front of
// every method; use it when building the grpc.Server the service is registered on.
func (s *Server) ServerOption() grpc.ServerOption { return grpc.ChainUnaryInterceptor(s.checkGrant) }

func (s *Server) checkGrant(ctx context.Context, req any, info *grpc.UnaryServerInfo, next grpc.UnaryHandler) (any, error) {
	if s.secret == "" {
		return next(ctx, req)
	}
	token := grants.BearerFromContext(ctx)
	if token == "" {
		slog.Warn("authz: call refused: no grant", "method", info.FullMethod)
		return nil, status.Error(codes.Unauthenticated, "a signed grant is required")
	}
	if _, err := grants.Verify(s.secret, token, grants.AudienceAuthz, timeNow()); err != nil {
		slog.Warn("authz: call refused", "method", info.FullMethod, "error", err)
		return nil, status.Error(codes.Unauthenticated, err.Error())
	}
	return next(ctx, req)
}

func identitiesOf(in []*pb.Identity) []Identity {
	out := make([]Identity, 0, len(in))
	for _, id := range in {
		out = append(out, Identity{Platform: id.GetPlatform(), Token: id.GetToken()})
	}
	return out
}

func factsOf(f *pb.RuleFacts) *Rule {
	if f == nil {
		return nil
	}
	return &Rule{
		Platform:     f.GetPlatform(),
		Datasource:   f.GetDatasource(),
		Include:      f.GetRepositories(),
		Exclude:      f.GetExcludeRepositories(),
		BranchMap:    f.GetBranchMap(),
		BranchAsEnv:  f.GetBranchAsEnv(),
		Branch:       f.GetBranch(),
		FilePathEnvs: f.GetFilePathEnvs(),
		Envs:         f.GetEnvs(),
		Tenants:      f.GetTenants(),
	}
}

// statusOf is the gRPC status of a question the engine could not answer.
func statusOf(err error) error {
	var ambiguous *AmbiguousRuleError
	var needPlatform *PlatformRequiredError
	var unknownPlatform *UnknownPlatformError
	var policy *PolicyError
	var ambiguousTenant *AmbiguousTenantError
	switch {
	case errors.Is(err, models.ErrTokenRejected):
		return status.Error(codes.Unauthenticated, err.Error())
	case errors.Is(err, ErrNoRule):
		return status.Error(codes.NotFound, err.Error())
	case errors.As(err, &ambiguous), errors.As(err, &needPlatform), errors.As(err, &unknownPlatform),
		errors.As(err, &ambiguousTenant):
		return status.Error(codes.InvalidArgument, err.Error())
	case errors.As(err, &policy):
		return status.Error(codes.Internal, err.Error())
	}
	// The platform could not be asked. The message is the error's own, which
	// holds a URL and a status, never a token.
	return status.Error(codes.Unavailable, "cannot ask the Git platform: "+err.Error())
}

// Identify implements pb.AuthzServer.
func (s *Server) Identify(ctx context.Context, in *pb.IdentifyRequest) (*pb.Who, error) {
	id := in.GetIdentity()
	who, err := s.engine.Identify(ctx, Identity{Platform: id.GetPlatform(), Token: id.GetToken()})
	if err != nil {
		return nil, statusOf(err)
	}
	return &pb.Who{Platform: who.Platform, Login: who.Login, Admin: who.Admin}, nil
}

// Decide implements pb.AuthzServer.
func (s *Server) Decide(ctx context.Context, in *pb.DecideRequest) (*pb.Decision, error) {
	d, err := s.engine.Decide(ctx, Request{
		Identities:     identitiesOf(in.GetIdentities()),
		Action:         Action(in.GetAction()),
		Rule:           factsOf(in.GetRule()),
		RulePlatform:   in.GetRulePlatform(),
		RuleDatasource: in.GetRuleDatasource(),
		Env:            in.GetEnv(),
		SourceRepo:     in.GetSourceRepo(),
		TenantID:       in.GetTenantId(),
		FilePath:       in.GetFilePath(),
		Via:            in.GetVia(),
		Resource:       in.GetResource(),
	})
	if err != nil {
		return nil, statusOf(err)
	}
	return &pb.Decision{
		Allowed:    d.Allowed,
		Reason:     d.Reason,
		DeniedBy:   d.DeniedBy,
		Login:      d.Login,
		Platform:   d.Platform,
		Admin:      d.Admin,
		Permission: d.Permission,
		Branch:     d.Branch,
		Tenant:     d.Tenant,
	}, nil
}

// ReadableRules implements pb.AuthzServer.
func (s *Server) ReadableRules(ctx context.Context, in *pb.ReadableRulesRequest) (*pb.ReadableRulesResponse, error) {
	rules := make([]Rule, 0, len(in.GetRules()))
	for _, f := range in.GetRules() {
		rules = append(rules, *factsOf(f))
	}
	got, err := s.engine.ReadableRules(ctx, identitiesOf(in.GetIdentities()), rules, in.GetTenantId())
	if err != nil {
		return nil, statusOf(err)
	}
	out := &pb.ReadableRulesResponse{Admin: got.Admin}
	for _, r := range got.Rules {
		out.Rules = append(out.Rules, &pb.RulePermission{Index: int32(r.Index), Permission: r.Permission, CanSync: r.CanSync})
	}
	return out, nil
}

// Forget implements pb.AuthzServer.
func (s *Server) Forget(_ context.Context, in *pb.ForgetRequest) (*emptypb.Empty, error) {
	s.engine.Forget(identitiesOf(in.GetIdentities()))
	return &emptypb.Empty{}, nil
}
