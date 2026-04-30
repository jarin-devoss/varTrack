"""
app/monitoring/config.py
─────────────────────────
Python dataclasses that mirror the gateway's proto monitoring models:

  PrometheusConfig      ↔  vartrack.v1.PrometheusConfig      (prometheus.pb.go)
  JaegerConfig          ↔  vartrack.v1.JaegerConfig          (jaeger.pb.go)
  OTelConfig            ↔  vartrack.v1.OTelConfig             (otel.pb.go)
  ElasticsearchConfig   ↔  vartrack.v1.ElasticsearchConfig   (elk.pb.go)
  LogstashConfig        ↔  vartrack.v1.LogstashConfig        (elk.pb.go)
  ELKConfig             ↔  vartrack.v1.ELKConfig             (elk.pb.go)
  MonitoringConfig      ↔  top-level bundle.monitoring section

Field names and defaults are kept identical to the proto definitions so
the CUE bundle and gateway config are transferable without translation.

Loading from env vars
──────────────────────
MonitoringConfig.from_env() reads each field from a prefixed env var:

  PROM_ENABLED=true          → prometheus.enabled
  PROM_PORT=9090             → prometheus.port
  PROM_METRICS_PATH=/metrics → prometheus.metrics_path
  PROM_PUSH_ENABLED=true     → prometheus.push_enabled
  PROM_PUSH_URL=http://...   → prometheus.push_url
  PROM_PUSH_INTERVAL=15      → prometheus.push_interval_seconds
  PROM_ENABLE_TLS=true       → prometheus.enable_tls
  PROM_TLS_CA=/path/ca.pem   → prometheus.tls_ca
  PROM_BASIC_AUTH_USER=...   → prometheus.basic_auth_user
  PROM_BASIC_AUTH_PASS=...   → prometheus.basic_auth_pass
  PROM_EXTERNAL_LABELS=k=v,k2=v2 → prometheus.external_labels

  JAEGER_ENABLED=true        → jaeger.enabled
  JAEGER_ENDPOINT=host:4317  → jaeger.endpoint
  JAEGER_PROTOCOL=grpc       → jaeger.protocol (grpc|thrift_http)
  JAEGER_SERVICE_NAME=orch   → jaeger.service_name
  JAEGER_SERVICE_VERSION=...  → jaeger.service_version
  JAEGER_ENVIRONMENT=prod    → jaeger.environment
  JAEGER_SAMPLER_TYPE=parentbased_always_on → jaeger.sampler_type
  JAEGER_SAMPLER_PARAM=1.0   → jaeger.sampler_param
  JAEGER_USE_TLS=true        → jaeger.use_tls
  JAEGER_AUTH_TOKEN=...      → jaeger.auth_token

  OTEL_ENABLED=true          → otel.enabled
  OTEL_ENDPOINT=host:4317    → otel.endpoint
  OTEL_PROTOCOL=grpc         → otel.protocol
  OTEL_SERVICE_NAME=orch     → otel.service_name
  OTEL_ENABLE_TRACES=true    → otel.enable_traces
  OTEL_ENABLE_METRICS=true   → otel.enable_metrics
  OTEL_SAMPLER_TYPE=...      → otel.sampler_type
  OTEL_METRICS_INTERVAL=60   → otel.metrics_interval_seconds
  OTEL_USE_TLS=true          → otel.use_tls
  OTEL_HEADERS=k=v,k2=v2    → otel.headers
  OTEL_PROPAGATORS=tracecontext,baggage → otel.propagators
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(val: str) -> bool:
    return val.strip().lower() in ("1", "true", "yes")


def _labels(val: str) -> dict[str, str]:
    """Parse 'k=v,k2=v2' into {'k': 'v', 'k2': 'v2'}."""
    result: dict[str, str] = {}
    for pair in val.split(","):
        pair = pair.strip()
        if "=" in pair:
            k, _, v = pair.partition("=")
            result[k.strip()] = v.strip()
    return result


# ── PrometheusConfig ──────────────────────────────────────────────────────────

@dataclass
class PrometheusConfig:
    """
    Configuration for the Prometheus scrape endpoint and optional Pushgateway.
    Mirrors gateway's vartrack.v1.PrometheusConfig proto message.
    """
    enabled:              bool             = False
    port:                 int              = 9090          # separate port (gateway pattern)
    metrics_path:         str              = "/metrics"
    external_labels:      dict[str, str]   = field(default_factory=dict)
    push_enabled:         bool             = False
    push_url:             str              = ""
    push_interval_seconds: float           = 15.0
    push_job_name:        str              = "orchestrator"
    enable_tls:           bool             = False
    tls_ca:               str              = ""
    tls_cert:             str              = ""
    tls_key:              str              = ""
    insecure_skip_verify: bool             = False
    basic_auth_user:      str              = ""
    basic_auth_pass:      str              = ""

    @classmethod
    def from_env(cls) -> "PrometheusConfig":
        return cls(
            enabled               = _bool(os.getenv("PROM_ENABLED", "false")),
            port                  = int(os.getenv("PROM_PORT", "9090")),
            metrics_path          = os.getenv("PROM_METRICS_PATH", "/metrics"),
            external_labels       = _labels(os.getenv("PROM_EXTERNAL_LABELS", "")),
            push_enabled          = _bool(os.getenv("PROM_PUSH_ENABLED", "false")),
            push_url              = os.getenv("PROM_PUSH_URL", ""),
            push_interval_seconds = float(os.getenv("PROM_PUSH_INTERVAL", "15")),
            push_job_name         = os.getenv("PROM_JOB_NAME", "orchestrator"),
            enable_tls            = _bool(os.getenv("PROM_ENABLE_TLS", "false")),
            tls_ca                = os.getenv("PROM_TLS_CA", ""),
            tls_cert              = os.getenv("PROM_TLS_CERT", ""),
            tls_key               = os.getenv("PROM_TLS_KEY", ""),
            insecure_skip_verify  = _bool(os.getenv("PROM_INSECURE_SKIP_VERIFY", "false")),
            basic_auth_user       = os.getenv("PROM_BASIC_AUTH_USER", ""),
            basic_auth_pass       = os.getenv("PROM_BASIC_AUTH_PASS", ""),
        )


# ── JaegerConfig ──────────────────────────────────────────────────────────────

@dataclass
class JaegerConfig:
    """
    Configuration for the Jaeger OTLP trace exporter.
    Mirrors gateway's vartrack.v1.JaegerConfig proto message.
    """
    enabled:              bool             = False
    endpoint:             str              = "localhost:4317"
    protocol:             str              = "grpc"          # grpc | thrift_http
    service_name:         str              = "vartrack-orchestrator"
    service_version:      str              = "dev"
    environment:          str              = ""
    sampler_type:         str              = "parentbased_always_on"
    sampler_param:        float            = 1.0
    max_queue_size:       int              = 0               # 0 = SDK default
    flush_interval_seconds: float          = 0.0             # 0 = SDK default
    use_tls:              bool             = False
    tls_ca_cert:          str              = ""
    tls_client_cert:      str              = ""
    tls_client_key:       str              = ""
    insecure_skip_verify: bool             = False
    auth_token:           str              = ""
    resource_attributes:  dict[str, str]   = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "JaegerConfig":
        return cls(
            enabled               = _bool(os.getenv("JAEGER_ENABLED", "false")),
            endpoint              = os.getenv("JAEGER_ENDPOINT", "localhost:4317"),
            protocol              = os.getenv("JAEGER_PROTOCOL", "grpc"),
            service_name          = os.getenv("JAEGER_SERVICE_NAME", "vartrack-orchestrator"),
            service_version       = os.getenv("JAEGER_SERVICE_VERSION", "dev"),
            environment           = os.getenv("JAEGER_ENVIRONMENT", ""),
            sampler_type          = os.getenv("JAEGER_SAMPLER_TYPE", "parentbased_always_on"),
            sampler_param         = float(os.getenv("JAEGER_SAMPLER_PARAM", "1.0")),
            max_queue_size        = int(os.getenv("JAEGER_MAX_QUEUE_SIZE", "0")),
            flush_interval_seconds= float(os.getenv("JAEGER_FLUSH_INTERVAL", "0")),
            use_tls               = _bool(os.getenv("JAEGER_USE_TLS", "false")),
            tls_ca_cert           = os.getenv("JAEGER_TLS_CA_CERT", ""),
            tls_client_cert       = os.getenv("JAEGER_TLS_CLIENT_CERT", ""),
            tls_client_key        = os.getenv("JAEGER_TLS_CLIENT_KEY", ""),
            insecure_skip_verify  = _bool(os.getenv("JAEGER_INSECURE_SKIP_VERIFY", "false")),
            auth_token            = os.getenv("JAEGER_AUTH_TOKEN", ""),
            resource_attributes   = _labels(os.getenv("JAEGER_RESOURCE_ATTRIBUTES", "")),
        )


# ── OTelConfig ────────────────────────────────────────────────────────────────

@dataclass
class OTelConfig:
    """
    Configuration for the OTLP generic exporter (Grafana Tempo, Honeycomb, etc.).
    Mirrors gateway's vartrack.v1.OTelConfig proto message.
    """
    enabled:                bool             = False
    endpoint:               str              = "localhost:4317"
    protocol:               str              = "grpc"          # grpc | http/protobuf | http/json
    service_name:           str              = "vartrack-orchestrator"
    service_version:        str              = "dev"
    environment:            str              = ""
    enable_traces:          bool             = True
    enable_metrics:         bool             = True
    sampler_type:           str              = "parentbased_always_on"
    sampler_ratio:          float            = 1.0
    batch_timeout_seconds:  float            = 0.0             # 0 = SDK default
    max_queue_size:         int              = 0
    max_export_batch:       int              = 0
    metrics_interval_seconds: float          = 60.0
    use_tls:                bool             = False
    tls_ca_cert:            str              = ""
    tls_client_cert:        str              = ""
    tls_client_key:         str              = ""
    insecure_skip_verify:   bool             = False
    headers:                dict[str, str]   = field(default_factory=dict)
    propagators:            list[str]        = field(default_factory=lambda: ["tracecontext", "baggage"])
    resource_attributes:    dict[str, str]   = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "OTelConfig":
        return cls(
            enabled                 = _bool(os.getenv("OTEL_ENABLED", "false")),
            endpoint                = os.getenv("OTEL_ENDPOINT", "localhost:4317"),
            protocol                = os.getenv("OTEL_PROTOCOL", "grpc"),
            service_name            = os.getenv("OTEL_SERVICE_NAME", "vartrack-orchestrator"),
            service_version         = os.getenv("OTEL_SERVICE_VERSION", "dev"),
            environment             = os.getenv("OTEL_ENVIRONMENT", ""),
            enable_traces           = _bool(os.getenv("OTEL_ENABLE_TRACES", "true")),
            enable_metrics          = _bool(os.getenv("OTEL_ENABLE_METRICS", "true")),
            sampler_type            = os.getenv("OTEL_SAMPLER_TYPE", "parentbased_always_on"),
            sampler_ratio           = float(os.getenv("OTEL_SAMPLER_RATIO", "1.0")),
            batch_timeout_seconds   = float(os.getenv("OTEL_BATCH_TIMEOUT", "0")),
            max_queue_size          = int(os.getenv("OTEL_MAX_QUEUE_SIZE", "0")),
            max_export_batch        = int(os.getenv("OTEL_MAX_EXPORT_BATCH", "0")),
            metrics_interval_seconds= float(os.getenv("OTEL_METRICS_INTERVAL", "60")),
            use_tls                 = _bool(os.getenv("OTEL_USE_TLS", "false")),
            tls_ca_cert             = os.getenv("OTEL_TLS_CA_CERT", ""),
            tls_client_cert         = os.getenv("OTEL_TLS_CLIENT_CERT", ""),
            tls_client_key          = os.getenv("OTEL_TLS_CLIENT_KEY", ""),
            insecure_skip_verify    = _bool(os.getenv("OTEL_INSECURE_SKIP_VERIFY", "false")),
            headers                 = _labels(os.getenv("OTEL_HEADERS", "")),
            propagators             = [p.strip() for p in os.getenv("OTEL_PROPAGATORS", "tracecontext,baggage").split(",") if p.strip()],
            resource_attributes     = _labels(os.getenv("OTEL_RESOURCE_ATTRIBUTES", "")),
        )


# ── ElasticsearchConfig ───────────────────────────────────────────────────────

@dataclass
class ElasticsearchConfig:
    """
    Elasticsearch connection config — sub-message of ELKConfig.
    Mirrors gateway's vartrack.v1.ElasticsearchConfig proto message.

    Auth priority (matches gateway esClient):
      api_key > bearer_token > username/password
    """
    endpoints:            list[str]        = field(default_factory=lambda: ["http://localhost:9200"])
    index:                str              = "vartrack-logs"
    pipeline:             str              = ""            # ingest pipeline name
    username:             str              = ""
    password:             str              = ""
    api_key:              str              = ""
    bearer_token:         str              = ""
    tls_ca_cert:          str              = ""
    tls_client_cert:      str              = ""
    tls_client_key:       str              = ""
    insecure_skip_verify: bool             = False
    bulk_max_docs:        int              = 500           # flush when buffer reaches this
    flush_interval_seconds: float          = 5.0
    extra_fields:         dict[str, str]   = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> "ElasticsearchConfig":
        raw_endpoints = os.getenv("ES_ENDPOINTS", "http://localhost:9200")
        return cls(
            endpoints            = [e.strip() for e in raw_endpoints.split(",") if e.strip()],
            index                = os.getenv("ES_INDEX", "vartrack-logs"),
            pipeline             = os.getenv("ES_PIPELINE", ""),
            username             = os.getenv("ES_USERNAME", ""),
            password             = os.getenv("ES_PASSWORD", ""),
            api_key              = os.getenv("ES_API_KEY", ""),
            bearer_token         = os.getenv("ES_BEARER_TOKEN", ""),
            tls_ca_cert          = os.getenv("ES_TLS_CA_CERT", ""),
            tls_client_cert      = os.getenv("ES_TLS_CLIENT_CERT", ""),
            tls_client_key       = os.getenv("ES_TLS_CLIENT_KEY", ""),
            insecure_skip_verify = _bool(os.getenv("ES_INSECURE_SKIP_VERIFY", "false")),
            bulk_max_docs        = int(os.getenv("ES_BULK_MAX_DOCS", "500")),
            flush_interval_seconds = float(os.getenv("ES_FLUSH_INTERVAL", "5")),
            extra_fields         = _labels(os.getenv("ES_EXTRA_FIELDS", "")),
        )


# ── LogstashConfig ────────────────────────────────────────────────────────────

@dataclass
class LogstashConfig:
    """
    Logstash HTTP input config — optional sub-message of ELKConfig.
    Mirrors gateway's vartrack.v1.LogstashConfig proto message.

    When configured, the ELKBackend routes log records through Logstash
    instead of directly to Elasticsearch.
    """
    endpoint:             str  = "localhost:5044"
    use_tls:              bool = False
    tls_ca_cert:          str  = ""
    tls_client_cert:      str  = ""
    tls_client_key:       str  = ""
    insecure_skip_verify: bool = False
    timeout_seconds:      float = 10.0

    @classmethod
    def from_env(cls) -> "LogstashConfig | None":
        """Returns None when LOGSTASH_ENDPOINT is not set (Logstash is optional)."""
        endpoint = os.getenv("LOGSTASH_ENDPOINT", "")
        if not endpoint:
            return None
        return cls(
            endpoint             = endpoint,
            use_tls              = _bool(os.getenv("LOGSTASH_USE_TLS", "false")),
            tls_ca_cert          = os.getenv("LOGSTASH_TLS_CA_CERT", ""),
            tls_client_cert      = os.getenv("LOGSTASH_TLS_CLIENT_CERT", ""),
            tls_client_key       = os.getenv("LOGSTASH_TLS_CLIENT_KEY", ""),
            insecure_skip_verify = _bool(os.getenv("LOGSTASH_INSECURE_SKIP_VERIFY", "false")),
            timeout_seconds      = float(os.getenv("LOGSTASH_TIMEOUT", "10")),
        )


# ── ELKConfig ─────────────────────────────────────────────────────────────────

@dataclass
class ELKConfig:
    """
    Top-level ELK backend config.
    Mirrors gateway's vartrack.v1.ELKConfig proto message.

    Log flow (gateway elk.go pattern):
      structlog JSON handler → ELKHandler → buffer → flush worker
        ├─ ES Bulk API      (when logstash is None)
        └─ Logstash HTTP    (when logstash is configured)

    The ELKBackend installs itself as a second structlog processor that fans
    out every log record to both stdout (preserved) and ES/Logstash.
    """
    enabled:         bool                         = False
    service_name:    str                          = "vartrack-orchestrator"
    service_version: str                          = "dev"
    environment:     str                          = ""
    elasticsearch:   ElasticsearchConfig          = field(default_factory=ElasticsearchConfig)
    logstash:        LogstashConfig | None        = None

    @classmethod
    def from_env(cls) -> "ELKConfig":
        return cls(
            enabled         = _bool(os.getenv("ELK_ENABLED", "false")),
            service_name    = os.getenv("ELK_SERVICE_NAME", "vartrack-orchestrator"),
            service_version = os.getenv("ELK_SERVICE_VERSION", "dev"),
            environment     = os.getenv("ELK_ENVIRONMENT", ""),
            elasticsearch   = ElasticsearchConfig.from_env(),
            logstash        = LogstashConfig.from_env(),
        )


# ── MonitoringConfig ──────────────────────────────────────────────────────────

@dataclass
class MonitoringConfig:
    """
    Top-level monitoring configuration — mirrors the bundle.monitoring section.
    Each backend is optional; disabled backends are no-ops.
    """
    prometheus: PrometheusConfig = field(default_factory=PrometheusConfig)
    jaeger:     JaegerConfig     = field(default_factory=JaegerConfig)
    otel:       OTelConfig       = field(default_factory=OTelConfig)
    elk:        ELKConfig        = field(default_factory=ELKConfig)

    @classmethod
    def from_env(cls) -> "MonitoringConfig":
        """
        Build a MonitoringConfig from environment variables.
        Called by init() when no explicit config is passed.
        """
        return cls(
            prometheus = PrometheusConfig.from_env(),
            jaeger     = JaegerConfig.from_env(),
            otel       = OTelConfig.from_env(),
            elk        = ELKConfig.from_env(),
        )
