package config

import (
	"net/url"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// cue writes a config.cue holding the datasources and the election setting.
func cue(t *testing.T, election, datasources string) string {
	t.Helper()
	body := "bundle: {\n"
	if election != "" {
		body += "\twatcher_leader_election_datasource: \"" + election + "\"\n"
	}
	body += "\tdatasources: [" + datasources + "]\n}\n"
	file := filepath.Join(t.TempDir(), "config.cue")
	if err := os.WriteFile(file, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	return file
}

func TestRedisElectionWithNoPassword(t *testing.T) {
	cfg, err := LeaderElection(cue(t, "redis-broker", `{redis: {tag: "broker", hosts: ["redis:6379"], database: 4}}`))
	if err != nil || cfg == nil {
		t.Fatalf("cfg=%v err=%v", cfg, err)
	}
	if cfg.RedisURL != "redis://redis:6379/4" || len(cfg.ZKHosts) != 0 {
		t.Errorf("RedisURL = %q", cfg.RedisURL)
	}
}

func TestTheElectionPasswordCanBeInlineOrAFile(t *testing.T) {
	dir := t.TempDir()
	plain := filepath.Join(dir, "redis-password")
	env := filepath.Join(dir, "redis.env")
	if err := os.WriteFile(plain, []byte("from-a-file\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(env, []byte("# redis\nREDIS_PASSWORD=from-a-key\nOTHER=x\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	quote := func(s string) string { return `"` + filepath.ToSlash(s) + `"` }

	for name, tc := range map[string]struct{ password, want string }{
		"inline":          {`"inline-pw"`, "inline-pw"},
		"the whole file":  {`{file: ` + quote(plain) + `}`, "from-a-file"},
		"a key of a file": {`{file: ` + quote(env) + `, key: "REDIS_PASSWORD"}`, "from-a-key"},
	} {
		t.Run(name, func(t *testing.T) {
			cfg, err := LeaderElection(cue(t, "redis", `{redis: {hosts: ["redis:6379"], password: `+tc.password+`}}`))
			if err != nil || cfg == nil {
				t.Fatalf("cfg=%v err=%v", cfg, err)
			}
			u, err := url.Parse(cfg.RedisURL)
			if err != nil {
				t.Fatal(err)
			}
			if got, _ := u.User.Password(); got != tc.want {
				t.Errorf("password = %q, want %q (url %s)", got, tc.want, cfg.RedisURL)
			}
		})
	}
}

func TestAPasswordWithSpecialCharactersSurvivesTheURL(t *testing.T) {
	cfg, err := LeaderElection(cue(t, "redis", `{redis: {hosts: ["redis:6379"], password: "p@ss/w:rd#1"}}`))
	if err != nil {
		t.Fatal(err)
	}
	u, err := url.Parse(cfg.RedisURL)
	if err != nil {
		t.Fatalf("%q is not a URL: %v", cfg.RedisURL, err)
	}
	if got, _ := u.User.Password(); got != "p@ss/w:rd#1" || u.Host != "redis:6379" {
		t.Errorf("password = %q, host = %q", got, u.Host)
	}
}

func TestARefPasswordStopsTheWatcherInsteadOfBeingDroppedSilently(t *testing.T) {
	_, err := LeaderElection(cue(t, "redis",
		`{redis: {hosts: ["redis:6379"], password: {ref: {path: "redis/main", key: "password"}}}}`))
	if err == nil || !strings.Contains(err.Error(), "{ref}") || !strings.Contains(err.Error(), "{file}") {
		t.Fatalf("err = %v, want it to say a {ref} needs a secret manager and to offer {value} or {file}", err)
	}
}

func TestAMissingPasswordFileStopsTheWatcher(t *testing.T) {
	_, err := LeaderElection(cue(t, "redis", `{redis: {hosts: ["redis:6379"], password: {file: "/nonexistent/redis-password"}}}`))
	if err == nil || !strings.Contains(err.Error(), "secret file") {
		t.Fatalf("err = %v, want the unreadable secret file named", err)
	}
}

func TestZooKeeperElection(t *testing.T) {
	cfg, err := LeaderElection(cue(t, "zookeeper", `{zookeeper: {hosts: ["zk1:2181", "zk2:2181"]}}`))
	if err != nil || cfg == nil {
		t.Fatalf("cfg=%v err=%v", cfg, err)
	}
	if len(cfg.ZKHosts) != 2 || cfg.ZKPath != "/vartrack/watcher/election" || cfg.RedisURL != "" {
		t.Errorf("cfg = %+v", cfg)
	}
}

func TestNoElectionWhenNoneIsConfiguredOrItCannotBeUsed(t *testing.T) {
	redis := `{redis: {tag: "broker", hosts: ["redis:6379"]}}`
	for name, file := range map[string]string{
		"none named":             cue(t, "", redis),
		"a datasource not found": cue(t, "redis-elsewhere", redis),
		"an unsupported type":    cue(t, "mongo", `{mongo: {endpoint: "mongodb://mongo:27017", database: "v"}}`),
	} {
		t.Run(name, func(t *testing.T) {
			cfg, err := LeaderElection(file)
			if cfg != nil || err != nil {
				t.Errorf("cfg=%v err=%v, want the watcher to run standalone", cfg, err)
			}
		})
	}
	if cfg, err := LeaderElection(""); cfg != nil || err != nil {
		t.Errorf("no config path: cfg=%v err=%v", cfg, err)
	}
}

func TestABundleThatCannotBeReadStopsTheWatcher(t *testing.T) {
	if _, err := LeaderElection(filepath.Join(t.TempDir(), "missing.cue")); err == nil {
		t.Error("a missing config.cue was taken for no election")
	}
}

func TestTheWatcherIgnoresSettingsOfOtherServices(t *testing.T) {
	// The gateway reads strictly; the watcher reads only election, and must keep
	// working when a rule gets a setting it does not know.
	file := filepath.Join(t.TempDir(), "config.cue")
	body := `bundle: {
	watcher_leader_election_datasource: "redis"
	datasources: [{redis: {hosts: ["redis:6379"]}}]
	rules: [{platform: "github", datasource: "redis", a_setting_from_the_future: true}]
}
`
	if err := os.WriteFile(file, []byte(body), 0o600); err != nil {
		t.Fatal(err)
	}
	cfg, err := LeaderElection(file)
	if err != nil || cfg == nil {
		t.Fatalf("cfg=%v err=%v", cfg, err)
	}
}

func TestSinkType(t *testing.T) {
	for name, want := range map[string]string{
		"redis": "redis", "redis-broker": "redis", "zookeeper-main": "zookeeper",
		"linux_server-web": "linux_server", "mongo-primary": "mongo", "redisx": "redisx",
	} {
		if got := sinkType(name); got != want {
			t.Errorf("sinkType(%q) = %q, want %q", name, got, want)
		}
	}
}
