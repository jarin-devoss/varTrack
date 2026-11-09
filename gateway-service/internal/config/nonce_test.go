package config

import (
	"context"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"gateway-service/internal/models"
	pbm "vartrack/libs/gen/vartrack/v1/models"
	pbds "vartrack/libs/gen/vartrack/v1/models/datasources"
	pbu "vartrack/libs/gen/vartrack/v1/utils"
)

func nonceBundle(name string, redis *pbds.RedisConfig) *models.Bundle {
	return models.NewBundle(&pbm.Bundle{
		GatewayNonceDatasource: name,
		Datasources:            []*pbm.DataSource{{Config: &pbm.DataSource_Redis{Redis: redis}}},
	})
}

func tag(s string) *string { return &s }

// A password with the characters a URL cannot carry: the options are built field by field, so it arrives as written.
func TestAPasswordWithUrlCharactersConnectsAsWritten(t *testing.T) {
	for _, password := range []string{"p@ss", "a:b", "w/rd@ra2", "100%", "x#y?z"} {
		redis := &pbds.RedisConfig{Tag: tag("broker"), Hosts: []string{"redis:6379"},
			Password: &pbu.SecretRef{Source: &pbu.SecretRef_Value{Value: password}}}
		name, opts, err := NonceRedisOptions(context.Background(), nonceBundle("redis-broker", redis))
		if err != nil || name != "redis-broker" {
			t.Fatalf("%q: %v (name %q)", password, err, name)
		}
		if opts.Password != password || opts.Addr != "redis:6379" {
			t.Fatalf("%q: options = %+v", password, opts)
		}
	}
}

func TestAPasswordGivenAsAFileIsRead(t *testing.T) {
	path := filepath.Join(t.TempDir(), "redis-password")
	if err := os.WriteFile(path, []byte("from-the-file\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	redis := &pbds.RedisConfig{Hosts: []string{"redis:6379"},
		Password: &pbu.SecretRef{Source: &pbu.SecretRef_File{File: path}}}
	_, opts, err := NonceRedisOptions(context.Background(), nonceBundle("redis", redis))
	if err != nil || opts.Password != "from-the-file" {
		t.Fatalf("got %+v, %v", opts, err)
	}
}

func TestAPasswordThatCannotBeReadIsAnErrorNotNoPassword(t *testing.T) {
	redis := &pbds.RedisConfig{Hosts: []string{"redis:6379"},
		Password: &pbu.SecretRef{Source: &pbu.SecretRef_File{File: filepath.Join(t.TempDir(), "missing")}}}
	_, opts, err := NonceRedisOptions(context.Background(), nonceBundle("redis", redis))
	if err == nil || opts != nil || !strings.Contains(err.Error(), "password") {
		t.Fatalf("got %+v, %v; want an error about the password", opts, err)
	}
}

func TestADatasourceThatIsNamedButNotThereIsAnError(t *testing.T) {
	redis := &pbds.RedisConfig{Tag: tag("other"), Hosts: []string{"redis:6379"}}
	_, _, err := NonceRedisOptions(context.Background(), nonceBundle("redis-broker", redis))
	if err == nil || !strings.Contains(err.Error(), `no redis datasource "redis-broker"`) {
		t.Fatalf("err = %v", err)
	}
}

func TestNoDatasourceNamedIsNotAnError(t *testing.T) {
	name, opts, err := NonceRedisOptions(context.Background(), nonceBundle("", &pbds.RedisConfig{Hosts: []string{"r:1"}}))
	if name != "" || opts != nil || err != nil {
		t.Fatalf("got %q, %+v, %v", name, opts, err)
	}
}
