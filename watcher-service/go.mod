module watcher-service

go 1.26.0

require (
	github.com/go-zookeeper/zk v1.0.4
	github.com/prometheus/client_golang v1.25.0
	github.com/prometheus/common v0.74.0
	github.com/redis/go-redis/v9 v9.23.0
	go.opentelemetry.io/otel v1.47.0
	go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc v1.47.0
	go.opentelemetry.io/otel/sdk v1.47.0
	go.opentelemetry.io/otel/trace v1.47.0
	golang.org/x/sync v0.25.0
	google.golang.org/grpc v1.85.0
	google.golang.org/protobuf v1.36.12
)

require (
	buf.build/gen/go/bufbuild/protovalidate/protocolbuffers/go v1.36.12-20260825204119-511051f7f437.2 // indirect
	buf.build/go/protovalidate v1.4.1 // indirect
	cel.dev/cel-go v0.33.2 // indirect
	cel.dev/expr v0.25.3 // indirect
	cuelang.org/go v0.18.0 // indirect
	github.com/antlr4-go/antlr/v4 v4.13.1 // indirect
	github.com/goccy/go-yaml v1.19.3-0.20260407131736-edee2f91616c // indirect
	github.com/pandatix/go-abnf v0.5.5 // indirect
	golang.org/x/exp v0.0.0-20261120130654-b6de8d37927a // indirect
	golang.org/x/mod v0.43.0 // indirect
	google.golang.org/genproto/googleapis/api v0.0.0-20260928230214-8a89bd6388cc // indirect
	google.golang.org/genproto/googleapis/rpc v0.0.0-20260928230214-8a89bd6388cc // indirect
)

require (
	cuelabs.dev/go/oci/ociregistry v0.0.0-20261026160121-545c0455168d // indirect
	github.com/beorn7/perks v1.0.1 // indirect
	github.com/cenkalti/backoff/v5 v5.0.3 // indirect
	github.com/cespare/xxhash/v2 v2.3.0 // indirect
	github.com/cockroachdb/apd/v3 v3.2.4-0.20261022180432-e9c7a71a8579 // indirect
	github.com/emicklei/proto v1.14.3 // indirect
	github.com/go-logr/logr v1.4.4 // indirect
	github.com/go-logr/stdr v1.2.2 // indirect
	github.com/google/uuid v1.6.0 // indirect
	github.com/grpc-ecosystem/grpc-gateway/v2 v2.30.0 // indirect
	github.com/mitchellh/go-wordwrap v1.0.1 // indirect
	github.com/munnerz/goautoneg v0.0.0-20191010083416-a7dc8b61c822 // indirect
	github.com/opencontainers/go-digest v1.0.1-0.20261015205711-63db54280247 // indirect
	github.com/opencontainers/image-spec v1.1.1 // indirect
	github.com/pelletier/go-toml/v2 v2.4.3 // indirect
	github.com/prometheus/client_model v0.6.3 // indirect
	github.com/prometheus/procfs v0.22.0 // indirect
	github.com/protocolbuffers/txtpbfmt v0.0.0-20261013103202-7c8a61a7a736 // indirect
	github.com/rogpeppe/go-internal v1.16.1-0.20261005065351-38c169d642f5 // indirect
	go.opentelemetry.io/auto/sdk v1.2.1 // indirect
	go.opentelemetry.io/otel/exporters/otlp/otlptrace v1.47.0 // indirect
	go.opentelemetry.io/otel/log v1.47.0 // indirect
	go.opentelemetry.io/otel/metric v1.47.0 // indirect
	go.opentelemetry.io/proto/otlp v1.11.0 // indirect
	go.uber.org/atomic v1.12.0 // indirect
	go.yaml.in/yaml/v3 v3.0.6 // indirect
	golang.org/x/net v0.63.0 // indirect
	golang.org/x/oauth2 v0.38.0 // indirect
	golang.org/x/sys v0.50.0 // indirect
	golang.org/x/text v0.44.0 // indirect
	vartrack/libs v0.0.0
)

replace vartrack/libs => ../libs/go
