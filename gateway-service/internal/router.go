package internal

import (
	"context"
	"net/http"
	"sync"

	"gateway-service/internal/auth"
	"gateway-service/internal/handlers"
	"gateway-service/internal/middlewares"
	"gateway-service/internal/models"
	"gateway-service/internal/routes"

	pb "gateway-service/internal/gen/proto/go/vartrack/v1/services"
)

// Router wires together the HTTP mux, middleware stack, health handler,
// rate limiters, and circuit breaker.
type Router struct {
	mux           *http.ServeMux
	bundleService *models.Bundle
	grpcClient    pb.OrchestratorClient
	grpcConn      handlers.GRPCConnChecker
	limiter       *middlewares.RateLimiter
	keyedLimiter  *middlewares.KeyedRateLimiter
	breaker       *middlewares.CircuitBreaker
	nonceStore    middlewares.NonceStore // nil = in-process memory store
	healthHandler *handlers.HealthHandler
	handler       http.Handler

	// CLI auth — nil when OIDC is not configured in the bundle.
	jwtValidator *auth.JWTValidator
	rbacEnforcer *auth.Enforcer
	opaEvaluator *auth.OPAEvaluator // nil when OPA is not configured

	isProduction bool
}

// RouterOption configures optional Router behaviour.
type RouterOption func(*Router)

// WithRateLimiterConfig overrides the default rate limiter settings.
func WithRateLimiterConfig(cfg middlewares.RateLimiterConfig) RouterOption {
	return func(r *Router) {
		r.limiter = middlewares.NewRateLimiter(cfg)
	}
}

// WithCircuitBreakerConfig overrides the default circuit breaker settings.
func WithCircuitBreakerConfig(cfg middlewares.CircuitBreakerConfig) RouterOption {
	return func(r *Router) {
		r.breaker = middlewares.NewCircuitBreaker(cfg)
	}
}

// WithKeyedRateLimiterConfig overrides the per-datasource rate limiter.
func WithKeyedRateLimiterConfig(cfg middlewares.KeyedRateLimiterConfig) RouterOption {
	return func(r *Router) {
		r.keyedLimiter = middlewares.NewKeyedRateLimiter(cfg)
	}
}

// WithNonceStore sets a distributed nonce store (e.g. *middlewares.RedisNonceStore).
// Required for multi-replica deployments to prevent cross-replica replay attacks.
func WithNonceStore(store middlewares.NonceStore) RouterOption {
	return func(r *Router) {
		r.nonceStore = store
	}
}

// WithProduction enables production-mode enforcement: HMAC signature
// verification and replay protection are required on all webhook requests.
// In non-production mode these checks are skipped so developers can POST
// webhooks without a valid secret.
func WithProduction(isProduction bool) RouterOption {
	return func(r *Router) {
		r.isProduction = isProduction
	}
}

// WithCLIAuth enables the /v1/cli/* routes with JWT validation and RBAC
// enforcement.  When this option is not provided the CLI routes are disabled.
func WithCLIAuth(jwtValidator *auth.JWTValidator, rbacEnforcer *auth.Enforcer, opa *auth.OPAEvaluator) RouterOption {
	return func(r *Router) {
		r.jwtValidator = jwtValidator
		r.rbacEnforcer = rbacEnforcer
		r.opaEvaluator = opa
	}
}

// NewRouter constructs the Router and wires all dependencies.
//
// All secret managers declared in the bundle are registered with the health
// handler so the readiness probe can call Ping() on each one.
func NewRouter(
	bundleService *models.Bundle,
	grpcClient pb.OrchestratorClient,
	grpcConn handlers.GRPCConnChecker,
	opts ...RouterOption,
) *Router {
	r := &Router{
		mux:           http.NewServeMux(),
		bundleService: bundleService,
		grpcClient:    grpcClient,
		grpcConn:      grpcConn,
		healthHandler: handlers.NewHealthHandler(grpcConn, grpcClient),
	}

	for _, o := range opts {
		o(r)
	}

	if r.limiter == nil {
		r.limiter = middlewares.NewRateLimiter(middlewares.DefaultRateLimiterConfig())
	}
	if r.keyedLimiter == nil {
		r.keyedLimiter = middlewares.NewKeyedRateLimiter(middlewares.DefaultKeyedRateLimiterConfig())
	}
	if r.breaker == nil {
		r.breaker = middlewares.NewCircuitBreaker(middlewares.DefaultCircuitBreakerConfig())
	}

	// Register all configured secret managers with the health handler so
	// the readiness probe can check them via Ping().
	for _, name := range bundleService.ListConfiguredSecretManagers() {
		r.healthHandler.RegisterSecretManager(name,
			&lazySecretManagerPinger{bundle: bundleService, name: name})
	}

	r.setupRoutes()
	r.buildMiddlewareChain()
	return r
}

// HealthHandler returns the shared health handler.
func (r *Router) HealthHandler() *handlers.HealthHandler {
	return r.healthHandler
}

// SetUnavailable marks the server as shutting down.
func (r *Router) SetUnavailable() {
	r.healthHandler.SetUnavailable()
}

func (r *Router) setupRoutes() {
	r.mux.Handle("/health/", http.StripPrefix("/health",
		routes.HealthRoutes(r.healthHandler)))

	r.mux.Handle("/webhooks/", http.StripPrefix("/webhooks",
		r.limiter.Middleware(
			r.keyedLimiter.Middleware(
				routes.WebhookRoutes(r.bundleService, r.grpcClient, r.breaker, r.nonceStore, r.isProduction),
			),
		),
	))

	// CLI routes are only registered when OIDC auth is configured in the bundle.
	if r.jwtValidator != nil && r.rbacEnforcer != nil {
		cliHandler := handlers.NewCLIHandler(r.grpcClient, r.opaEvaluator)
		jwtMiddleware := middlewares.JWTAuth(r.jwtValidator)
		r.mux.Handle("/v1/cli/", http.StripPrefix("/v1/cli",
			routes.CLIRoutes(cliHandler, jwtMiddleware, r.rbacEnforcer),
		))
	}
}

func (r *Router) buildMiddlewareChain() {
	// Outermost → innermost:
	//   Recovery → SecurityHeaders → RequestLog → RequestID → CorrelationID → mux
	var h http.Handler = r.mux
	h = middlewares.CorrelationID(h)
	h = middlewares.RequestID(h)
	h = middlewares.RequestLog(h)
	h = middlewares.SecurityHeaders(h)
	h = middlewares.Recovery()(h)
	r.handler = h
}

func (r *Router) ServeHTTP(w http.ResponseWriter, req *http.Request) {
	r.handler.ServeHTTP(w, req)
}

// lazySecretManagerPinger implements handlers.SecretManagerPinger by
// lazily resolving the secret manager from the bundle on first use and caching it.
type lazySecretManagerPinger struct {
	bundle *models.Bundle
	name   string

	mu sync.Mutex
	sm handlers.SecretManagerPinger
}

// Ping resolves the secret manager via the bundle (cached after first call)
// and delegates to its Ping method.
func (p *lazySecretManagerPinger) Ping(ctx context.Context) error {
	p.mu.Lock()
	if p.sm == nil {
		sm, err := p.bundle.GetSecretManager(ctx, p.name)
		if err != nil {
			p.mu.Unlock()
			return err
		}
		p.sm = sm
	}
	sm := p.sm
	p.mu.Unlock()

	pingErr := sm.Ping(ctx)
	if pingErr != nil {
		p.mu.Lock()
		if p.sm == sm { // only clear if nobody replaced it concurrently
			p.sm = nil
		}
		p.mu.Unlock()
	}
	return pingErr
}
