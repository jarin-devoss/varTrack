package models

import (
	"context"
	"fmt"

	pb_ds "vartrack/libs/gen/vartrack/v1/models/datasources"
	pb_utils "vartrack/libs/gen/vartrack/v1/utils"
)

// GatewayNonceDatasource is the name ("redis-broker") of the Redis datasource the gateway keeps webhook
// replay protection in; empty when config.cue names none.
func (s *Bundle) GatewayNonceDatasource() string {
	return s.bundle.GetGatewayNonceDatasource()
}

// RedisDatasource is the Redis datasource called name ("redis", or "redis-" and its tag), or an error
// when the bundle has none by that name.
func (s *Bundle) RedisDatasource(name string) (*pb_ds.RedisConfig, error) {
	for _, d := range s.bundle.GetDatasources() {
		r := d.GetRedis()
		if r == nil {
			continue
		}
		full := "redis"
		if r.GetTag() != "" {
			full = "redis-" + r.GetTag()
		}
		if full == name {
			return r, nil
		}
	}
	return nil, fmt.Errorf("the bundle has no redis datasource %q", name)
}

// ResolveSecret reads a SecretRef of the bundle (a plain value, a {file:} or a {ref:}), from the default
// secret manager when it is a {ref:}. The gateway's secret cache serves it for its TTL; a secret that
// cannot be read is an error, never an empty string.
func (s *Bundle) ResolveSecret(ctx context.Context, ref *pb_utils.SecretRef) (string, error) {
	if ref == nil {
		return "", nil
	}
	manager := ""
	if _, isRef := ref.GetSource().(*pb_utils.SecretRef_Ref); isRef {
		name, err := s.defaultSecretManagerName()
		if err != nil {
			return "", err
		}
		manager = name
	}
	return s.secretRefResolver.Resolve(ctx, ref, manager)
}
