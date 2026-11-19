package bundle

import (
	"strings"
	"testing"
)

const mongoPrimary = `{mongo: {tag: "primary", endpoint: "mongodb://m:27017", database: "v", collection: "c"}}`
const mongoDR = `{mongo: {tag: "dr", endpoint: "mongodb://m:27017", database: "v", collection: "c"}}`

// limitsBundle is a whole config with three datasources (mongo, mongo-primary, mongo-dr) and a sync_limits list.
func limitsBundle(limits string) string {
	return wholeBundle(`token: "t"`, mongoOK+`, `+mongoPrimary+`, `+mongoDR, ``) + "\n" + `bundle: sync_limits: ` + limits + "\n"
}

func TestSyncLimitsLoadAndKeepTheirValues(t *testing.T) {
	b, err := Load(writeCUE(t, limitsBundle(`[
		{name: "prod-mongo", datasources: ["mongo-primary", "mongo-dr"], max_at_once: 2},
		{name: "local",      datasources: ["mongo"],                      max_at_once: 1},
	]`)), Options{Strict: true})
	if err != nil {
		t.Fatalf("valid sync_limits were refused: %v", err)
	}
	limits := b.GetSyncLimits()
	if len(limits) != 2 {
		t.Fatalf("read %d limits, want 2", len(limits))
	}
	if limits[0].GetName() != "prod-mongo" || limits[0].GetMaxAtOnce() != 2 || len(limits[0].GetDatasources()) != 2 {
		t.Errorf("sync_limits[0] = %v", limits[0])
	}
}

func TestABundleWithoutSyncLimitsLoadsAsBefore(t *testing.T) {
	if err := loadWhole(t, wholeBundle(`token: "t"`, mongoOK, ``)); err != nil {
		t.Fatalf("a config with no sync_limits was refused: %v", err)
	}
}

func TestAMaxAtOnceBelowOneIsRefusedWithItsPath(t *testing.T) {
	err := loadWhole(t, limitsBundle(`[{name: "x", datasources: ["mongo"], max_at_once: 0}]`))
	if err == nil || !strings.Contains(err.Error(), "sync_limits[0].max_at_once") {
		t.Fatalf("max_at_once 0 was not refused with its path: %v", err)
	}
}

func TestALimitNeedsANameAndDatasources(t *testing.T) {
	if err := loadWhole(t, limitsBundle(`[{name: "", datasources: ["mongo"], max_at_once: 1}]`)); err == nil {
		t.Error("a limit with an empty name loaded")
	}
	if err := loadWhole(t, limitsBundle(`[{name: "x", datasources: [], max_at_once: 1}]`)); err == nil {
		t.Error("a limit with no datasources loaded")
	}
}

func TestTwoLimitsWithOneNameAreRefused(t *testing.T) {
	err := loadWhole(t, limitsBundle(`[
		{name: "same", datasources: ["mongo"],         max_at_once: 1},
		{name: "same", datasources: ["mongo-primary"], max_at_once: 1},
	]`))
	if err == nil || !strings.Contains(err.Error(), `sync_limits[1] repeats the name "same"`) {
		t.Fatalf("a repeated limit name was not refused: %v", err)
	}
}

func TestADatasourceInTwoLimitsIsRefusedNamingBoth(t *testing.T) {
	err := loadWhole(t, limitsBundle(`[
		{name: "a", datasources: ["mongo", "mongo-dr"], max_at_once: 1},
		{name: "b", datasources: ["mongo-dr"],          max_at_once: 2},
	]`))
	if err == nil {
		t.Fatal("a datasource in two limits loaded")
	}
	for _, want := range []string{"sync_limits[1] (b)", `"mongo-dr"`, "sync_limits[0] (a)", "at most one limit"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("the error does not say %q:\n%v", want, err)
		}
	}
}

func TestADatasourceTheBundleDoesNotDeclareIsRefusedWithWhatIsDeclared(t *testing.T) {
	err := loadWhole(t, limitsBundle(`[{name: "x", datasources: ["mongo-primery"], max_at_once: 1}]`))
	if err == nil {
		t.Fatal("a limit on a datasource the bundle does not declare loaded")
	}
	for _, want := range []string{`"mongo-primery"`, "does not declare", "mongo-primary", "mongo-dr"} {
		if !strings.Contains(err.Error(), want) {
			t.Errorf("the error does not say %q:\n%v", want, err)
		}
	}
}

func TestAFragmentMayNameDatasourcesItDoesNotShow(t *testing.T) {
	// A docs snippet cannot say which datasources exist.
	text := `bundle: sync_limits: [{name: "x", datasources: ["mongo-primary"], max_at_once: 2}]` + "\n"
	if _, err := Load(writeCUE(t, text), Options{Strict: true, Fragment: true}); err != nil {
		t.Fatalf("a fragment with sync_limits was refused: %v", err)
	}
}
