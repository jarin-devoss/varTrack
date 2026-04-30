package handlers

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"hash"
	"io"
	"log/slog"
	"net/http"
	"strings"
	"sync"
	"time"

	pb "gateway-service/internal/gen/proto/go/vartrack/v1/services"
	"gateway-service/internal/middlewares"
	"gateway-service/internal/models"
	mon "gateway-service/internal/monitoring"
	"gateway-service/internal/protoutil"

	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// defaultWebhookBodySize is the fallback limit used when the bundle does not
// configure max_webhook_body_bytes. The authoritative default lives in
// models.Bundle.WebhookBodyLimit().
const defaultWebhookBodySize = 10 << 20

var bufferPool = sync.Pool{
	New: func() interface{} {
		// Pre-allocate 64KB for moderate payloads, grows if needed.
		return bytes.NewBuffer(make([]byte, 0, 64*1024))
	},
}

// hashingReader intercepts reads to populate a hash and memory buffer simultaneously.
type hashingReader struct {
	r      io.Reader
	hasher hash.Hash
	buf    *bytes.Buffer
}

func (h *hashingReader) Read(p []byte) (n int, err error) {
	n, err = h.r.Read(p)
	if n > 0 {
		if h.hasher != nil {
			h.hasher.Write(p[:n])
		}
		if h.buf != nil {
			h.buf.Write(p[:n])
		}
	}
	return n, err
}

// WebhookHandler processes incoming webhook requests.
type WebhookHandler struct {
	bundleService   *models.Bundle
	client          pb.OrchestratorClient
	breaker         *middlewares.CircuitBreaker
	replayProtector *middlewares.ReplayProtector
	// skipValidation disables HMAC signature and replay-protection checks.
	// Enabled automatically in non-production environments (dev/test/staging).
	skipValidation bool
}

// NewWebhookHandler creates a new WebhookHandler.
//
// nonceStore is optional: when nil the handler uses an in-process memory
// store (suitable for single-replica deployments).  Pass a *middlewares.
// RedisNonceStore for multi-replica deployments so all replicas share the
// same nonce window and cross-replica replay attacks are blocked.
func NewWebhookHandler(
	bundleService *models.Bundle,
	client pb.OrchestratorClient,
	breaker *middlewares.CircuitBreaker,
	nonceStore middlewares.NonceStore,
	isProduction bool,
) *WebhookHandler {
	cfg := middlewares.DefaultReplayProtectionConfig()
	var rp *middlewares.ReplayProtector
	if nonceStore != nil {
		rp = middlewares.NewReplayProtectorWithStore(cfg, nonceStore)
	} else {
		rp = middlewares.NewReplayProtector(cfg)
	}
	return &WebhookHandler{
		bundleService:   bundleService,
		client:          client,
		breaker:         breaker,
		replayProtector: rp,
		skipValidation:  !isProduction,
	}
}

// Handle processes regular datasource webhooks (POST /webhooks/{datasource}).
func (h *WebhookHandler) Handle(w http.ResponseWriter, r *http.Request) {
	if !isJSONContentType(r) {
		writeErrorJSON(w, http.StatusUnsupportedMediaType,
			ErrCodeInvalidContentType, "Content-Type must be application/json")
		return
	}

	datasourceName := r.PathValue("datasource")
	if datasourceName == "" {
		writeErrorJSON(w, http.StatusBadRequest,
			ErrCodeMissingDatasource, "datasource name is required")
		return
	}

	cid := middlewares.GetCorrelationID(r.Context())
	rid := middlewares.GetRequestID(r.Context())
	receivedAt := timestamppb.New(time.Now().UTC()) // ← stamp receipt time

	slog.Info("webhook received",
		"datasource", datasourceName,
		"correlation_id", cid,
		"request_id", rid,
	)

	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	ctx = injectCorrelationID(ctx, cid)

	// ── OTel: root span covers the full webhook ingress including platform lookup ──
	ctx, webhookSpan := mon.Start(ctx, "webhook.process")
	webhookSpan.SetAttr("webhook.datasource", datasourceName)
	webhookSpan.SetAttr("correlation_id", cid)
	webhookSpan.SetAttr("request_id", rid)
	var webhookErr error
	defer func() { webhookSpan.End(webhookErr) }()

	platform, platformName, err := h.bundleService.GetPlatformForDatasource(ctx, datasourceName)
	if err != nil {
		slog.Warn("no platform for datasource",
			"datasource", datasourceName, "error", err,
			"correlation_id", cid, "request_id", rid)
		writeErrorJSON(w, http.StatusNotFound,
			ErrCodeDatasourceNotFound,
			fmt.Sprintf("no configuration found for datasource %q", datasourceName))
		webhookErr = err
		return
	}
	webhookSpan.SetAttr("webhook.platform", platformName)

	body, eventType, ok := h.verifyWebhook(w, r, platform, platformName, cid, rid)
	if !ok {
		webhookErr = fmt.Errorf("webhook verification failed")
		return
	}
	webhookSpan.SetAttr("webhook.event_type", eventType)

	if !platform.IsPushEvent(eventType) && !platform.IsPREvent(eventType) {
		slog.Info("ignoring unhandled event type",
			"event_type", eventType, "datasource", datasourceName,
			"correlation_id", cid, "request_id", rid)
		writeJSON(w, http.StatusOK, jsonResponse{Message: "event ignored"})
		return
	}

	if !h.breaker.Allow() {
		slog.Warn("circuit breaker open: failing fast",
			"datasource", datasourceName,
			"correlation_id", cid, "request_id", rid)
		writeErrorJSON(w, http.StatusServiceUnavailable,
			ErrCodeOrchestratorUnavailable,
			"orchestrator temporarily unavailable, please retry later")
		webhookErr = fmt.Errorf("circuit breaker open")
		return
	}

	// ── OTel: child span for the gRPC forward ──
	ctx, grpcSpan := mon.StartOrchestratorSpan(ctx, "ProcessWebhook")
	grpcSpan.SetAttr("webhook.datasource", datasourceName)
	var grpcErr error
	defer func() { grpcSpan.End(grpcErr) }()

	headers := flattenHeaders(r.Header)
	resp, err := h.client.ProcessWebhook(ctx, &pb.ProcessWebhookRequest{
		Platform:   platformName,
		Datasource: datasourceName,
		RawPayload: string(body),
		Headers:    headers,
		ReceivedAt: receivedAt, // ← new Timestamp field from improved proto
	})
	if err != nil {
		h.breaker.RecordFailure()
		grpcErr = err
		webhookErr = err
		slog.Error("failed to forward to orchestrator",
			"error", err, "correlation_id", cid, "request_id", rid)
		writeErrorJSON(w, http.StatusBadGateway,
			ErrCodeOrchestratorError, "failed to forward to orchestrator")
		return
	}
	h.breaker.RecordSuccess()
	grpcSpan.SetAttr("task_id", resp.GetTaskId())
	webhookSpan.SetAttr("task_id", resp.GetTaskId())
	writeJSON(w, http.StatusAccepted, jsonResponse{TaskID: resp.GetTaskId(), Message: resp.GetMessage()})
}

// HandleSchemaRegistry processes schema registry webhooks.
func (h *WebhookHandler) HandleSchemaRegistry(w http.ResponseWriter, r *http.Request) {
	if !isJSONContentType(r) {
		writeErrorJSON(w, http.StatusUnsupportedMediaType,
			ErrCodeInvalidContentType, "Content-Type must be application/json")
		return
	}

	schemaRegistry := h.bundleService.GetSchemaRegistry()
	if schemaRegistry == nil {
		slog.Warn("schema registry webhook received but no schema_registry configured in bundle")
		writeErrorJSON(w, http.StatusNotFound,
			ErrCodeSchemaRegistryNotConfigured, "schema registry not configured")
		return
	}

	platformName := schemaRegistry.GetPlatform()
	repo := schemaRegistry.GetRepo()
	branch := schemaRegistry.GetBranch()
	cid := middlewares.GetCorrelationID(r.Context())
	rid := middlewares.GetRequestID(r.Context())
	receivedAt := protoutil.NowProto() // ← use helper

	slog.Info("schema registry webhook received",
		"platform", platformName, "repo", repo, "branch", branch,
		"correlation_id", cid, "request_id", rid)

	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Second)
	defer cancel()
	ctx = injectCorrelationID(ctx, cid)

	managerName := schemaRegistry.GetSecretManager()
	platform, err := h.bundleService.GetPlatform(ctx, platformName, managerName)
	if err != nil {
		slog.Error("failed to get platform for schema registry",
			"platform", platformName, "error", err,
			"correlation_id", cid, "request_id", rid)
		writeErrorJSON(w, http.StatusInternalServerError,
			ErrCodePlatformResolutionFailed,
			fmt.Sprintf("failed to resolve platform %q", platformName))
		return
	}

	bodyString, eventType, ok := h.verifyWebhook(w, r, platform, platformName, cid, rid)
	if !ok {
		return
	}

	if !platform.IsPushEvent(eventType) {
		slog.Info("schema registry: ignoring non-push event",
			"event_type", eventType,
			"correlation_id", cid, "request_id", rid)
		writeJSON(w, http.StatusOK, jsonResponse{Message: "event ignored"})
		return
	}

	if !h.breaker.Allow() {
		slog.Warn("circuit breaker open: failing fast (schema-registry)",
			"correlation_id", cid, "request_id", rid)
		writeErrorJSON(w, http.StatusServiceUnavailable,
			ErrCodeOrchestratorUnavailable,
			"orchestrator temporarily unavailable, please retry later")
		return
	}

	payloadRepo, payloadBranch := extractRepoBranch(platformName, []byte(bodyString))
	if payloadRepo != "" && payloadRepo != repo {
		slog.Warn("schema registry payload repo mismatch",
			"expected", repo, "got", payloadRepo,
			"correlation_id", cid, "request_id", rid)
		writeErrorJSON(w, http.StatusBadRequest,
			ErrCodePayloadValidation,
			"webhook payload targets a different repository")
		return
	}
	if payloadBranch != "" && payloadBranch != branch {
		slog.Warn("schema registry payload branch mismatch",
			"expected", branch, "got", payloadBranch,
			"correlation_id", cid, "request_id", rid)
		writeErrorJSON(w, http.StatusBadRequest,
			ErrCodePayloadValidation,
			"webhook payload targets a different branch")
		return
	}

	headers := flattenHeaders(r.Header)
	resp, err := h.client.ProcessSchemaWebhook(ctx, &pb.ProcessSchemaWebhookRequest{
		Platform:   platformName,
		Repo:       repo,
		Branch:     branch,
		RawPayload: bodyString,
		Headers:    headers,
		ReceivedAt: receivedAt, // ← new Timestamp field
	})
	if err != nil {
		h.breaker.RecordFailure()
		slog.Error("failed to forward schema webhook to orchestrator",
			"error", err, "correlation_id", cid, "request_id", rid)
		writeErrorJSON(w, http.StatusBadGateway,
			ErrCodeOrchestratorError, "failed to forward to orchestrator")
		return
	}
	h.breaker.RecordSuccess()
	writeJSON(w, http.StatusAccepted, jsonResponse{TaskID: resp.GetTaskId(), Message: resp.GetMessage()})
}

// verifyWebhook performs header, body, signature, replay, JSON, and structural checks.
func (h *WebhookHandler) verifyWebhook(
	w http.ResponseWriter, r *http.Request,
	platform models.Platform, platformName, correlationID, requestID string,
) (bodyString string, eventType string, ok bool) {

	eventTypeHeader := platform.EventTypeHeader()
	eventType = r.Header.Get(eventTypeHeader)
	if eventType == "" {
		slog.Warn("platform mismatch: expected event-type header not present",
			"header", eventTypeHeader, "platform", platformName,
			"correlation_id", correlationID, "request_id", requestID)
		writeErrorJSON(w, http.StatusBadRequest,
			ErrCodePlatformMismatch,
			fmt.Sprintf("webhook source mismatch: expected platform %q (header %q missing)",
				platformName, eventTypeHeader))
		return "", "", false
	}

	buf := bufferPool.Get().(*bytes.Buffer)
	buf.Reset()
	defer func() {
		if buf.Cap() <= 128*1024 {
			buf.Reset()
			bufferPool.Put(buf)
		}
		// Oversized buffer: let the GC reclaim it.
	}()

	var mac hash.Hash
	secret := platform.Secret()
	if h.skipValidation {
		slog.Warn("webhook validation disabled (non-production mode) — skipping HMAC and replay checks",
			"platform", platformName, "correlation_id", correlationID)
	} else if secret != "" {
		mac = platform.WebhookHasher()
	}

	hr := &hashingReader{
		r:      http.MaxBytesReader(w, r.Body, h.bundleService.WebhookBodyLimit()),
		hasher: mac,
		buf:    buf,
	}

	if err := validateWebhookStream(platformName, eventType, hr); err != nil {
		slog.Warn("webhook payload failed structural validation",
			"platform", platformName, "event_type", eventType,
			"error", err,
			"correlation_id", correlationID, "request_id", requestID)

		if strings.HasPrefix(err.Error(), "failed to parse payload:") {
			writeErrorJSON(w, http.StatusBadRequest, ErrCodeInvalidJSON, "request body is not valid JSON")
		} else {
			writeErrorJSON(w, http.StatusBadRequest, ErrCodePayloadValidation, fmt.Sprintf("payload validation failed: %s", err.Error()))
		}
		return "", "", false
	}

	if _, err := io.Copy(io.Discard, hr); err != nil {
		slog.Error("failed to construct full webhook buffer",
			"error", err, "correlation_id", correlationID, "request_id", requestID)
		writeErrorJSON(w, http.StatusRequestEntityTooLarge, ErrCodeBodyReadFailed, "failed to read request body")
		return "", "", false
	}

	if !h.skipValidation && secret != "" {
		signatureHeader := r.Header.Get(platform.SignatureHeader())
		if !platform.VerifySignature(mac, signatureHeader) {
			slog.Warn("invalid webhook signature",
				"platform", platformName,
				"correlation_id", correlationID, "request_id", requestID)
			writeErrorJSON(w, http.StatusUnauthorized,
				ErrCodeSignatureInvalid, "invalid signature")
			return "", "", false
		}
	}

	if !h.skipValidation && h.replayProtector != nil {
		// Timestamp-based replay check (Slack, Stripe).
		extractor := resolveTimestampExtractor(platformName)
		if eventTime, extractErr := extractor(r); extractErr != nil {
			slog.Warn("failed to extract event timestamp",
				"platform", platformName, "error", extractErr,
				"correlation_id", correlationID, "request_id", requestID)
			writeErrorJSON(w, http.StatusBadRequest,
				ErrCodePayloadValidation,
				fmt.Sprintf("invalid event timestamp: %s", extractErr.Error()))
			return "", "", false
		} else if replayErr := h.replayProtector.Validate(eventTime); replayErr != nil {
			slog.Warn("replay attack detected",
				"platform", platformName, "error", replayErr,
				"correlation_id", correlationID, "request_id", requestID)
			writeErrorJSON(w, http.StatusUnauthorized,
				ErrCodeReplayDetected, "request rejected: "+replayErr.Error())
			return "", "", false
		}

		// Nonce-based replay check for GitHub and Gitea (no timestamp header).
		var deliveryHeader string
		switch platformName {
		case "github":
			deliveryHeader = "X-GitHub-Delivery"
		case "gitea":
			deliveryHeader = "X-Gitea-Delivery"
		}
		if deliveryHeader != "" {
			deliveryID := r.Header.Get(deliveryHeader)
			if !h.replayProtector.CheckNonce(deliveryID) {
				slog.Warn("replay detected: duplicate delivery ID",
					"platform", platformName,
					"delivery_id", deliveryID,
					"correlation_id", correlationID, "request_id", requestID)
				writeErrorJSON(w, http.StatusUnauthorized,
					ErrCodeReplayDetected, "duplicate delivery: request already processed")
				return "", "", false
			}
		}
	}

	return buf.String(), eventType, true
}

func resolveTimestampExtractor(platformName string) middlewares.TimestampExtractor {
	switch platformName {
	case "slack":
		return middlewares.ExtractSlackTimestamp
	case "stripe":
		return middlewares.ExtractStripeTimestamp
	default:
		return middlewares.ExtractGitHubTimestamp
	}
}

func validateWebhookStream(platformName, eventType string, r io.Reader) error {
	var payload struct {
		Ref         *string   `json:"ref"`
		Repository  *struct{} `json:"repository"`
		PullRequest *struct{} `json:"pull_request"`
		Action      *string   `json:"action"`
		Project     *struct{} `json:"project"`
		ObjectAttr  *struct{} `json:"object_attributes"`
		Push        *struct{} `json:"push"`
		Type        *string   `json:"type"`
		ID          *string   `json:"id"`
	}
	if err := json.NewDecoder(r).Decode(&payload); err != nil && err != io.EOF {
		return fmt.Errorf("failed to parse payload: %w", err)
	}

	switch platformName {
	case "github", "gitea":
		switch eventType {
		case "push":
			if payload.Ref == nil || payload.Repository == nil {
				return fmt.Errorf("missing required field 'ref' or 'repository' for %s/%s event", platformName, eventType)
			}
		case "pull_request":
			if payload.Action == nil || payload.PullRequest == nil || payload.Repository == nil {
				return fmt.Errorf("missing required field 'action', 'pull_request', or 'repository' for %s/%s event", platformName, eventType)
			}
		}
	case "gitlab":
		switch eventType {
		case "Push Hook", "Tag Push Hook":
			if payload.Ref == nil || payload.Project == nil {
				return fmt.Errorf("missing required field 'ref' or 'project' for %s/%s event", platformName, eventType)
			}
		case "Merge Request Hook":
			if payload.ObjectAttr == nil || payload.Project == nil {
				return fmt.Errorf("missing required field 'object_attributes' or 'project' for %s/%s event", platformName, eventType)
			}
		}
	case "bitbucket":
		switch eventType {
		case "repo:push":
			if payload.Push == nil || payload.Repository == nil {
				return fmt.Errorf("missing required field 'push' or 'repository' for %s/%s event", platformName, eventType)
			}
		}
	case "slack":
		if payload.Type == nil {
			return fmt.Errorf("missing required field 'type' for %s event", platformName)
		}
	case "stripe":
		if payload.ID == nil || payload.Type == nil {
			return fmt.Errorf("missing required field 'id' or 'type' for %s event", platformName)
		}
	default:
		return fmt.Errorf("unsupported platform for structural validation: %s", platformName)
	}

	return nil
}

func extractRepoBranch(platformName string, body []byte) (string, string) {
	var payload struct {
		Ref        string `json:"ref"`
		Repository struct {
			FullName string `json:"full_name"`
		} `json:"repository"`
		Project struct {
			PathWithNamespace string `json:"path_with_namespace"`
		} `json:"project"`
		Push struct {
			Changes []struct {
				New struct {
					Name string `json:"name"`
				} `json:"new"`
			} `json:"changes"`
		} `json:"push"`
	}
	_ = json.Unmarshal(body, &payload)

	var repo, branch string

	switch platformName {
	case "github", "gitea":
		repo = payload.Repository.FullName
		branch = strings.TrimPrefix(payload.Ref, "refs/heads/")
	case "gitlab":
		repo = payload.Project.PathWithNamespace
		branch = strings.TrimPrefix(payload.Ref, "refs/heads/")
	case "bitbucket":
		repo = payload.Repository.FullName
		if len(payload.Push.Changes) > 0 {
			branch = payload.Push.Changes[0].New.Name
		}
	}
	return repo, branch
}

type jsonResponse struct {
	TaskID  string `json:"task_id"`
	Message string `json:"message"`
}

func flattenHeaders(h http.Header) map[string]string {
	headers := make(map[string]string, len(h))
	for k, v := range h {
		if len(v) > 0 {
			headers[k] = strings.Join(v, ", ")
		}
	}
	return headers
}

func injectCorrelationID(ctx context.Context, id string) context.Context {
	if id == "" {
		return ctx
	}
	return metadata.AppendToOutgoingContext(ctx, middlewares.HeaderCorrelationID, id)
}

func isJSONContentType(r *http.Request) bool {
	ct := r.Header.Get("Content-Type")
	return strings.HasPrefix(strings.ToLower(strings.TrimSpace(ct)), "application/json")
}
