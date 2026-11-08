package main

import (
	"context"
	"strings"
	"testing"

	"gateway-service/internal/degraded"
	"gateway-service/internal/models"
	pbm "vartrack/libs/gen/vartrack/v1/models"
	pbds "vartrack/libs/gen/vartrack/v1/models/datasources"
)

func bundleNaming(name string, hosts ...string) *models.Bundle {
	b := &pbm.Bundle{GatewayNonceDatasource: name}
	if len(hosts) > 0 {
		b.Datasources = []*pbm.DataSource{{Config: &pbm.DataSource_Redis{Redis: &pbds.RedisConfig{Hosts: hosts}}}}
	}
	return models.NewBundle(b)
}

func TestTheGatewayDoesNotStartWithoutTheStoreItWasAskedToUse(t *testing.T) {
	t.Setenv("VARTRACK_ALLOW_DEGRADED", "")
	degraded.Reset()
	// named, and nothing listens there
	_, err := nonceStoreFor(context.Background(), bundleNaming("redis", "127.0.0.1:1"), false)
	if err == nil || !strings.Contains(err.Error(), "unreachable") || !strings.Contains(err.Error(), "VARTRACK_ALLOW_DEGRADED=nonce_store") {
		t.Fatalf("err = %v", err)
	}
	// named, and not in the bundle
	if _, err := nonceStoreFor(context.Background(), bundleNaming("redis-broker"), false); err == nil {
		t.Fatal("a datasource that is not there must stop the gateway")
	}
	if len(degraded.Active()) != 0 {
		t.Fatalf("nothing was allowed, so no mode may be on: %v", degraded.Active())
	}
}

func TestNoStoreNamedStopsProductionAndNeedsTheModeElsewhere(t *testing.T) {
	t.Setenv("VARTRACK_ALLOW_DEGRADED", "")
	degraded.Reset()
	if _, err := nonceStoreFor(context.Background(), bundleNaming(""), true); err == nil ||
		!strings.Contains(err.Error(), "production does not run without it") {
		t.Fatalf("production: %v", err)
	}
	if _, err := nonceStoreFor(context.Background(), bundleNaming(""), false); err == nil {
		t.Fatal("development without the mode named must stop too")
	}
}

func TestTheInProcessStoreIsOnlyForAnOperatorWhoNamedItAndIsListed(t *testing.T) {
	t.Setenv("VARTRACK_ALLOW_DEGRADED", "nonce_store")
	degraded.Reset()
	store, err := nonceStoreFor(context.Background(), bundleNaming("redis", "127.0.0.1:1"), false)
	if err != nil || store != nil {
		t.Fatalf("got %v, %v; want the in-process default (nil) and no error", store, err)
	}
	if why := degraded.Active()["nonce_store"]; !strings.Contains(why, "unreachable") {
		t.Fatalf("the mode must be listed with its reason: %v", degraded.Active())
	}
	// never in production, whatever is named
	degraded.Reset()
	if _, err := nonceStoreFor(context.Background(), bundleNaming("redis", "127.0.0.1:1"), true); err == nil {
		t.Fatal("production must refuse the in-process store even when it is named")
	}
	if len(degraded.Active()) != 0 {
		t.Fatalf("production took a mode it may not: %v", degraded.Active())
	}
}
