package models

import (
	"testing"

	pb "vartrack/libs/gen/vartrack/v1/models"
	pb_sm "vartrack/libs/gen/vartrack/v1/models/secret_managers"
)

func TestDefaultSecretManagerName(t *testing.T) {
	vault := func(tag string) *pb.SecretManager {
		cfg := &pb_sm.VaultConfig{Endpoint: "http://vault:8200"}
		if tag != "" {
			cfg.Tag = &tag
		}
		return &pb.SecretManager{Config: &pb.SecretManager_Vault{Vault: cfg}}
	}
	tests := []struct {
		name     string
		managers []*pb.SecretManager
		want     string
		wantErr  bool
	}{
		{"the only one", []*pb.SecretManager{vault("prod")}, "vault-prod", false},
		{"the one tagged default", []*pb.SecretManager{vault("prod"), vault("default")}, "vault-default", false},
		{"several, none tagged default", []*pb.SecretManager{vault("prod"), vault("dev")}, "", true},
		{"none", nil, "", true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			b := NewBundle(&pb.Bundle{SecretManagers: tc.managers})
			got, err := b.defaultSecretManagerName()
			if got != tc.want || (err != nil) != tc.wantErr {
				t.Errorf("= %q, %v; want %q, error %v", got, err, tc.want, tc.wantErr)
			}
		})
	}
}
