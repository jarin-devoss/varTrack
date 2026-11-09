package bundle

import (
	"strings"
	"testing"
)

// A bundle small enough to read, and whole: one platform, one datasource, one rule with its repositories.
func wholeBundle(platformExtra, datasources, ruleExtra string) string {
	return `bundle: {
	platforms: [{gitea: {endpoint: "http://gitea:3000", ` + platformExtra + `}}]
	datasources: [` + datasources + `]
	rules: [{platform: "gitea", datasource: "mongo", repositories: ["acme/configs"], ` + ruleExtra + `}]
}
`
}

const mongoOK = `{mongo: {endpoint: "mongodb://m:27017", database: "v", collection: "c"}}`

func loadWhole(t *testing.T, text string) error {
	t.Helper()
	_, err := Load(writeCUE(t, text), Options{Strict: true})
	return err
}

func TestAWholeConfigThatKeepsTheRulesLoads(t *testing.T) {
	if err := loadWhole(t, wholeBundle(`token: "t"`, mongoOK, ``)); err != nil {
		t.Fatalf("a valid config was refused: %v", err)
	}
}

// What plan 07 found: the protos' rules were run by nothing, so these two loaded.
func TestARedisDatabaseOutsideItsRangeIsRefusedWithItsPath(t *testing.T) {
	text := wholeBundle(`token: "t"`, mongoOK+`, {redis: {hosts: ["r:6379"], database: -5}}`, ``)
	err := loadWhole(t, text)
	if err == nil || !strings.Contains(err.Error(), "datasources[1].redis.database") {
		t.Fatalf("redis database -5 was not refused with its path: %v", err)
	}
}

func TestAMongoUsernameWithoutItsPasswordIsRefused(t *testing.T) {
	text := wholeBundle(`token: "t"`, `{mongo: {endpoint: "mongodb://m:27017", database: "v", collection: "c", username: "u"}}`, ``)
	err := loadWhole(t, text)
	if err == nil || !strings.Contains(err.Error(), "Password is required when username is provided") {
		t.Fatalf("a username without a password was not refused: %v", err)
	}
}

func TestEveryViolationIsReportedInOneError(t *testing.T) {
	text := wholeBundle(`token: "t"`,
		`{redis: {hosts: ["r:6379"], database: 99}}, {mongo: {endpoint: "mongodb://m:27017", database: "v", username: "u"}}`, ``)
	err := loadWhole(t, text)
	if err == nil {
		t.Fatal("a config with several broken rules loaded")
	}
	for _, want := range []string{"breaks 3 rules", "datasources[0].redis.database", "datasources[1].mongo"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("the error does not say %q:\n%v", want, err)
		}
	}
}

// A field the config leaves out is read as its default by the services, and several protos state that default as
// a rule (git_scm_signature must equal X-Gitea-Signature). The rules are for what the config sets.
func TestAFieldTheConfigLeavesOutIsNotHeldToItsRule(t *testing.T) {
	// no protocol, page_size, signature header, api_path, pool size, base_path, endpoint of an S3 …
	text := wholeBundle(`token: "t"`,
		mongoOK+`, {redis: {hosts: ["r:6379"]}}, {zookeeper: {hosts: ["z:2181"]}}, {s3: {bucket: "b"}}`, ``)
	if err := loadWhole(t, text); err != nil {
		t.Fatalf("unset fields were held to their rules: %v", err)
	}
}

func TestWhatTheProtoRequiresIsRequiredEvenWhenLeftOut(t *testing.T) {
	// a datasource with no hosts, a rule with no repositories, a bundle with nothing in it
	for name, c := range map[string]struct{ text, want string }{
		"redis hosts":  {wholeBundle(`token: "t"`, `{redis: {database: 1}}`, ``), "datasources[0].redis.hosts"},
		"repositories": {`bundle: {platforms: [{gitea: {endpoint: "http://g"}}], datasources: [` + mongoOK + `], rules: [{platform: "gitea", datasource: "mongo"}]}`, "rules[0].repositories"},
		"no platforms": {`bundle: {datasources: [` + mongoOK + `], rules: [{platform: "gitea", datasource: "mongo", repositories: ["a/b"]}]}`, "platforms"},
		"no rules":     {`bundle: {platforms: [{gitea: {endpoint: "http://g"}}], datasources: [` + mongoOK + `]}`, "rules"},
	} {
		err := loadWhole(t, c.text)
		if err == nil || !strings.Contains(err.Error(), c.want) {
			t.Errorf("%s: want a refusal naming %q, got %v", name, c.want, err)
		}
	}
}

// A fragment (a snippet in the docs) is a piece of a bundle: nothing is demanded of what it leaves out.
func TestAFragmentIsNotAskedToBeAWholeBundleButItsContentIsChecked(t *testing.T) {
	file := writeCUE(t, `bundle: datasources: [{redis: {hosts: ["r:6379"]}}]`+"\n")
	if _, err := Load(file, Options{Strict: true}); err == nil {
		t.Fatal("a piece of a bundle loaded as a whole one")
	}
	if _, err := Load(file, Options{Strict: true, Fragment: true}); err != nil {
		t.Fatalf("a fragment was refused for what it leaves out: %v", err)
	}
	bad := writeCUE(t, `bundle: datasources: [{redis: {hosts: ["r:6379"], database: -1}}]`+"\n")
	if _, err := Load(bad, Options{Strict: true, Fragment: true}); err == nil {
		t.Fatal("a fragment's own value was not checked")
	}
}

// config.cue holds the first layer of a rule: file_name comes from the tenant's bundle.json, so a rule that
// names neither file_name nor file_path_map is a layer, not a mistake.
func TestARuleLayerWithoutAFileNameLoads(t *testing.T) {
	if err := loadWhole(t, wholeBundle(`token: "t"`, mongoOK, ``)); err != nil {
		t.Fatalf("the rules of a layer were held to the merged rule's rules: %v", err)
	}
}

func TestOptionsTheProtosRelaxedAreOptional(t *testing.T) {
	// a bundle without schema_registry (tenants declare theirs), a Gitea with no protocol, an S3 with no
	// endpoint (AWS), a ZooKeeper with no base_path (the rule's destination_template is the path)
	text := wholeBundle(`token: "t"`, mongoOK+`, {s3: {bucket: "b"}}, {zookeeper: {hosts: ["z:1"]}}`, ``)
	if err := loadWhole(t, text); err != nil {
		t.Fatal(err)
	}
}

// What the sinks used to refuse only at their first push, a config refuses when it loads.
func TestARuleThatNeededASinkToFailIsRefusedWhenTheConfigLoads(t *testing.T) {
	for name, c := range map[string]struct{ datasource, want string }{
		"an S3 with no bucket":               {`{s3: {endpoint: "http://minio:9000"}}`, "datasources[1].s3.bucket"},
		"accept-new with nowhere to record":  {`{linux_server: {host: "h", username: "u", password: "p", file_paths: ["/etc/a"], host_key_verification: "accept-new"}}`, "accept-new needs known_hosts_file"},
		"a ZooKeeper that stores a document": {`{zookeeper: {hosts: ["z:2181"], update_strategy: "STRATEGY_DOCUMENT"}}`, "one znode per key"},
	} {
		err := loadWhole(t, wholeBundle(`token: "t"`, mongoOK+`, `+c.datasource, ``))
		if err == nil || !strings.Contains(err.Error(), c.want) {
			t.Errorf("%s: want a refusal naming %q, got %v", name, c.want, err)
		}
	}
}

func TestTheSameSettingsThatAreRightLoad(t *testing.T) {
	right := `{s3: {bucket: "b", endpoint: "http://minio:9000"}},
		{linux_server: {host: "h", username: "u", password: "p", file_paths: ["/etc/a"], host_key_verification: "accept-new", known_hosts_file: "/etc/kh"}},
		{zookeeper: {hosts: ["z:2181"], update_strategy: "STRATEGY_KEY_VALUE"}},
		{zookeeper: {tag: "unset", hosts: ["z:2181"]}}`
	if err := loadWhole(t, wholeBundle(`token: "t"`, mongoOK+`, `+right, ``)); err != nil {
		t.Fatalf("valid settings were refused: %v", err)
	}
}
