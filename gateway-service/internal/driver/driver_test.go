package driver_test

import (
	"context"
	"errors"
	"testing"

	"gateway-service/internal/driver"
)

// --- Registry tests ---

type fakeDriver struct {
	name   string
	opened bool
}

type fakeConfig struct {
	name string
}

func fakeOpener(d *fakeDriver, _ context.Context, cfg fakeConfig) (*fakeDriver, error) {
	d.name = cfg.name
	d.opened = true
	return d, nil
}

func TestRegistry_OpenRegisteredDriver(t *testing.T) {
	r := driver.NewRegistry[*fakeDriver, fakeConfig]("test", fakeOpener)
	r.Register("alpha", func() *fakeDriver { return &fakeDriver{} })

	d, err := r.Open(context.Background(), "alpha", fakeConfig{name: "a1"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !d.opened {
		t.Error("expected driver to be opened")
	}
	if d.name != "a1" {
		t.Errorf("got name %q, want %q", d.name, "a1")
	}
}

func TestRegistry_OpenUnknownDriver(t *testing.T) {
	r := driver.NewRegistry[*fakeDriver, fakeConfig]("test", fakeOpener)

	_, err := r.Open(context.Background(), "missing", fakeConfig{})
	if err == nil {
		t.Fatal("expected error for unknown driver")
	}
}

func TestRegistry_RegisterNilPanics(t *testing.T) {
	r := driver.NewRegistry[*fakeDriver, fakeConfig]("test", fakeOpener)

	defer func() {
		if rec := recover(); rec == nil {
			t.Fatal("expected panic for nil registration")
		}
	}()
	r.Register("bad", nil)
}

func TestRegistry_RegisterDuplicatePanics(t *testing.T) {
	r := driver.NewRegistry[*fakeDriver, fakeConfig]("test", fakeOpener)
	r.Register("dup", func() *fakeDriver { return &fakeDriver{} })

	defer func() {
		if rec := recover(); rec == nil {
			t.Fatal("expected panic for duplicate registration")
		}
	}()
	r.Register("dup", func() *fakeDriver { return &fakeDriver{} })
}

// --- Factory tests ---

func TestFactory_Get_Success(t *testing.T) {
	r := driver.NewRegistry[*fakeDriver, fakeConfig]("test", fakeOpener)
	r.Register("bravo", func() *fakeDriver { return &fakeDriver{} })

	f := driver.NewFactory(
		r,
		func(c fakeConfig) string { return c.name },
		nil, // no validation
		"test",
	)

	d, err := f.Get(context.Background(), fakeConfig{name: "bravo"})
	if err != nil {
		t.Fatalf("unexpected error: %v", err)
	}
	if !d.opened {
		t.Error("expected driver to be opened")
	}
}

func TestFactory_Get_ValidationError(t *testing.T) {
	r := driver.NewRegistry[*fakeDriver, fakeConfig]("test", fakeOpener)

	f := driver.NewFactory(
		r,
		func(c fakeConfig) string { return c.name },
		func(c fakeConfig) error { return errors.New("bad config") },
		"test",
	)

	_, err := f.Get(context.Background(), fakeConfig{name: "any"})
	if err == nil {
		t.Fatal("expected validation error")
	}
}

func TestFactory_Get_EmptyName(t *testing.T) {
	r := driver.NewRegistry[*fakeDriver, fakeConfig]("test", fakeOpener)

	f := driver.NewFactory(
		r,
		func(c fakeConfig) string { return "" },
		nil,
		"test",
	)

	_, err := f.Get(context.Background(), fakeConfig{})
	if err == nil {
		t.Fatal("expected error for empty name")
	}
}

// --- ResolveTagName tests ---

func TestResolveTagName(t *testing.T) {
	tests := []struct {
		typeName string
		tag      string
		want     string
	}{
		{"github", "dr", "github-dr"},
		{"github", "", "github"},
		{"vault", "prod", "vault-prod"},
		{"vault", "", "vault"},
	}

	for _, tt := range tests {
		got := driver.ResolveTagName(tt.typeName, tt.tag)
		if got != tt.want {
			t.Errorf("ResolveTagName(%q, %q) = %q, want %q",
				tt.typeName, tt.tag, got, tt.want)
		}
	}
}
