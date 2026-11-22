package bundle

import (
	"os"
	"path/filepath"
	"testing"
	"time"
)

// config.cue is read by every service when it starts, and a bundle is sometimes someone else's file (a copied
// example, a tenant's change that has not been looked at yet). Load answers a bundle or an error for any text;
// it never panics, strictly or not. The examples are the seeds.
//
//	go test -run '^$' -fuzz '^FuzzLoad$' -fuzztime 30s ./bundle
func FuzzLoad(f *testing.F) {
	seeds, _ := filepath.Glob("../../../examples/bundles/*.cue")
	for _, s := range seeds {
		if b, err := os.ReadFile(s); err == nil {
			f.Add(b, true)
		}
	}
	f.Add([]byte(`bundle: {}`), true)
	f.Add([]byte(`bundle: {rules: [{platform: "github", datasource: "mongo"}]}`), false)
	f.Add([]byte(`bundle: {datasources: [{redis: {database: 99}}]}`), true)
	f.Add([]byte(``), false)
	f.Add([]byte(`{{{{`), true)

	dir := f.TempDir()
	f.Fuzz(func(t *testing.T, cue []byte, strict bool) {
		path := filepath.Join(dir, "config.cue")
		if err := os.WriteFile(path, cue, 0o600); err != nil {
			t.Skip(err)
		}
		done := make(chan struct{})
		go func() {
			defer close(done)
			_, _ = Load(path, Options{Strict: strict})
		}()
		select {
		case <-done:
		case <-time.After(10 * time.Second):
			t.Fatalf("Load took more than 10 s on %d bytes of CUE:\n%s", len(cue), cue)
		}
	})
}
