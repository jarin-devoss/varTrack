package models_test

import (
	"testing"

	pb "gateway-service/internal/gen/proto/go/vartrack/v1/models"
	pb_ds "gateway-service/internal/gen/proto/go/vartrack/v1/models/datasources"
	pb_gh "gateway-service/internal/gen/proto/go/vartrack/v1/models/platforms"
	pb_sm "gateway-service/internal/gen/proto/go/vartrack/v1/models/secret_managers"
	"gateway-service/internal/models"
)

func strPtr(s string) *string { return &s }

func TestRule_InclusionPatterns(t *testing.T) {
	tests := []struct {
		name string
		rule *pb.Rule
		want []string
	}{
		{
			name: "base repos only",
			rule: &pb.Rule{
				Repositories: []string{"org/repo-a", "org/repo-b"},
			},
			want: []string{"org/repo-a", "org/repo-b"},
		},
		{
			name: "with enabled override",
			rule: &pb.Rule{
				Repositories: []string{"org/repo-a"},
				Overrides: []*pb.RepositoryOverride{
					{Enable: true, MatchRepositories: []string{"org/repo-c"}},
				},
			},
			want: []string{"org/repo-a", "org/repo-c"},
		},
		{
			name: "disabled override excluded",
			rule: &pb.Rule{
				Repositories: []string{"org/repo-a"},
				Overrides: []*pb.RepositoryOverride{
					{Enable: false, MatchRepositories: []string{"org/repo-c"}},
				},
			},
			want: []string{"org/repo-a"},
		},
		{
			name: "empty rule",
			rule: &pb.Rule{},
			want: []string{},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			r := &models.Rule{Rule: tt.rule}
			got := r.InclusionPatterns()
			if len(got) != len(tt.want) {
				t.Fatalf("got %v, want %v", got, tt.want)
			}
			for i, v := range got {
				if v != tt.want[i] {
					t.Errorf("index %d: got %q, want %q", i, v, tt.want[i])
				}
			}
		})
	}
}

func TestRule_ExclusionPatterns(t *testing.T) {
	tests := []struct {
		name string
		rule *pb.Rule
		want []string
	}{
		{
			name: "base exclusions only",
			rule: &pb.Rule{
				ExcludeRepositories: []string{"org/excluded"},
			},
			want: []string{"org/excluded"},
		},
		{
			name: "with enabled override",
			rule: &pb.Rule{
				ExcludeRepositories: []string{"org/excluded-a"},
				Overrides: []*pb.RepositoryOverride{
					{Enable: true, ExcludeRepositories: []string{"org/excluded-b"}},
				},
			},
			want: []string{"org/excluded-a", "org/excluded-b"},
		},
		{
			name: "disabled override excluded",
			rule: &pb.Rule{
				ExcludeRepositories: []string{"org/excluded-a"},
				Overrides: []*pb.RepositoryOverride{
					{Enable: false, ExcludeRepositories: []string{"org/excluded-b"}},
				},
			},
			want: []string{"org/excluded-a"},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			r := &models.Rule{Rule: tt.rule}
			got := r.ExclusionPatterns()
			if len(got) != len(tt.want) {
				t.Fatalf("got %v, want %v", got, tt.want)
			}
			for i, v := range got {
				if v != tt.want[i] {
					t.Errorf("index %d: got %q, want %q", i, v, tt.want[i])
				}
			}
		})
	}
}

func TestPlatformName(t *testing.T) {
	tests := []struct {
		name     string
		platform *pb.Platform
		want     string
	}{
		{
			name: "github no tag",
			platform: &pb.Platform{
				Config: &pb.Platform_Github{
					Github: &pb_gh.GitHub{},
				},
			},
			want: "github",
		},
		{
			name: "github with tag",
			platform: &pb.Platform{
				Config: &pb.Platform_Github{
					Github: &pb_gh.GitHub{Tag: strPtr("dr")},
				},
			},
			want: "github-dr",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := models.PlatformName(tt.platform)
			if got != tt.want {
				t.Errorf("PlatformName() = %q, want %q", got, tt.want)
			}
		})
	}
}

func TestPlatformDriverName(t *testing.T) {
	p := &pb.Platform{
		Config: &pb.Platform_Github{
			Github: &pb_gh.GitHub{Tag: strPtr("prod")},
		},
	}
	got := models.PlatformDriverName(p)
	if got != "github" {
		t.Errorf("PlatformDriverName() = %q, want %q", got, "github")
	}
}

func TestSecretManagerName(t *testing.T) {
	tests := []struct {
		name string
		sm   *pb.SecretManager
		want string
	}{
		{
			name: "vault no tag",
			sm: &pb.SecretManager{
				Config: &pb.SecretManager_Vault{
					Vault: &pb_sm.VaultConfig{},
				},
			},
			want: "vault",
		},
		{
			name: "vault with tag",
			sm: &pb.SecretManager{
				Config: &pb.SecretManager_Vault{
					Vault: &pb_sm.VaultConfig{Tag: strPtr("prod")},
				},
			},
			want: "vault-prod",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := models.SecretManagerName(tt.sm)
			if got != tt.want {
				t.Errorf("SecretManagerName() = %q, want %q", got, tt.want)
			}
		})
	}
}

func TestDataSourceName(t *testing.T) {
	tests := []struct {
		name string
		ds   *pb.DataSource
		want string
	}{
		{
			name: "mongo no tag",
			ds: &pb.DataSource{
				Config: &pb.DataSource_Mongo{
					Mongo: &pb_ds.MongoConfig{},
				},
			},
			want: "mongo",
		},
		{
			name: "mongo with tag",
			ds: &pb.DataSource{
				Config: &pb.DataSource_Mongo{
					Mongo: &pb_ds.MongoConfig{Tag: strPtr("analytics")},
				},
			},
			want: "mongo-analytics",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := models.DataSourceName(tt.ds)
			if got != tt.want {
				t.Errorf("DataSourceName() = %q, want %q", got, tt.want)
			}
		})
	}
}
