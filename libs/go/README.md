# libs/go — Go code the gateway and the watcher share

One module, `vartrack/libs`. The gateway and the watcher each `require` it and
`replace` it with this directory (see their `go.mod`), so there is one copy
of everything below, not one per service.

| Path | What it is |
|---|---|
| `gen/` | The Go protobuf and gRPC stubs, written by `buf generate` from `proto/` (see `buf.gen.yaml` at the repo root). Never edit by hand. |
| `gen/buf/validate/validate.pb.go` | The protovalidate stubs the other stubs import. Vendored: `buf generate` does not write this one, so it is the only file in `gen/` that stays when the rest is regenerated. |

Import paths start with `vartrack/libs/`, for example
`vartrack/libs/gen/vartrack/v1/models`.
