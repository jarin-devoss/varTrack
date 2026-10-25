package bundle

import (
	"testing"

	"google.golang.org/protobuf/encoding/protojson"

	pb_models "vartrack/libs/gen/vartrack/v1/models"
)

func TestSecretShorthandFollowsTheProtoNotTheFieldName(t *testing.T) {
	// Every credential field takes a plain string. A Redis or S3 credential
	// is a SecretRef, so the string is expanded; a MongoDB password and a
	// Vault token are plain strings in their protos, so they stay as they are.
	in := []byte(`{
		"platforms": [{"github": {"endpoint": "https://github.com", "token": "ghp_x", "secret": {"file": "/run/secrets/hook"}}}],
		"datasources": [
			{"mongo": {"endpoint": "mongodb://mongo:27017", "database": "vartrack", "username": "vt", "password": "mongo-pw"}},
			{"redis": {"tag": "cache", "password": "redis-pw"}},
			{"s3": {"bucket": "b", "access_key_id": "AKIA", "secret_access_key": {"ref": {"path": "aws", "key": "secret"}}}},
			{"linux_server": {"host": "h", "username": "deploy", "ssh_key": {"private_key": "-----BEGIN"}}}
		],
		"secret_managers": [{"vault": {"endpoint": "http://vault:8200", "mount_point": "secret", "kv_version": 2, "token_auth": {"token": "root"}}}]
	}`)
	normalized, err := normalizeSecretRefs(in)
	if err != nil {
		t.Fatal(err)
	}
	b := &pb_models.Bundle{}
	if err := protojson.Unmarshal(normalized, b); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}

	ds := b.GetDatasources()
	checks := []struct {
		name, got, want string
	}{
		{"github token", b.GetPlatforms()[0].GetGithub().GetToken().GetValue(), "ghp_x"},
		{"github secret file", b.GetPlatforms()[0].GetGithub().GetSecret().GetFile(), "/run/secrets/hook"},
		{"mongo password", ds[0].GetMongo().GetPassword(), "mongo-pw"},
		{"redis password", ds[1].GetRedis().GetPassword().GetValue(), "redis-pw"},
		{"s3 access key", ds[2].GetS3().GetAccessKeyId().GetValue(), "AKIA"},
		{"s3 secret ref", ds[2].GetS3().GetSecretAccessKey().GetRef().GetPath(), "aws"},
		{"linux key in a nested message", ds[3].GetLinuxServer().GetSshKey().GetPrivateKey().GetValue(), "-----BEGIN"},
		{"linux username", ds[3].GetLinuxServer().GetUsername(), "deploy"},
		{"vault token", b.GetSecretManagers()[0].GetVault().GetTokenAuth().GetToken(), "root"},
	}
	for _, c := range checks {
		if c.got != c.want {
			t.Errorf("%s = %q, want %q", c.name, c.got, c.want)
		}
	}
}

func TestTheFileKeyFormLoads(t *testing.T) {
	in := []byte(`{"platforms": [{"github": {"endpoint": "https://github.com",
		"secret": {"file": "/run/secrets/github.env", "key": "WEBHOOK_SECRET"}}}]}`)
	normalized, err := normalizeSecretRefs(in)
	if err != nil {
		t.Fatal(err)
	}
	b := &pb_models.Bundle{}
	if err := protojson.Unmarshal(normalized, b); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	s := b.GetPlatforms()[0].GetGithub().GetSecret()
	if s.GetFile() != "/run/secrets/github.env" || s.GetKey() != "WEBHOOK_SECRET" {
		t.Errorf("secret = %v", s)
	}
}
