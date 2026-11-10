package bundle

import (
	"fmt"
	"strings"
	"testing"

	enums "vartrack/libs/gen/vartrack/v1/enums"
)

func syncModeCUE(value string) string {
	return fmt.Sprintf(`bundle: rules: [{platform: "github", datasource: "a", sync_mode: %s}]`, value)
}

func TestSyncModeTakesTheDocumentedShortNamesAndTheProtoNames(t *testing.T) {
	tests := []struct {
		value string
		want  enums.SyncMode
	}{
		{`"AUTO"`, enums.SyncMode_SYNC_MODE_AUTO},
		{`"auto"`, enums.SyncMode_SYNC_MODE_AUTO},
		{`"SYNC_MODE_AUTO"`, enums.SyncMode_SYNC_MODE_AUTO},
		{`"GIT_SMART_REPAIR"`, enums.SyncMode_SYNC_MODE_GIT_SMART_REPAIR},
		{`"SYNC_MODE_GIT_UPSERT_ALL"`, enums.SyncMode_SYNC_MODE_GIT_UPSERT_ALL},
		{`"GIT_UPSERT_ALL"`, enums.SyncMode_SYNC_MODE_GIT_UPSERT_ALL},
		{`1`, enums.SyncMode_SYNC_MODE_GIT_UPSERT_ALL},
		{`4`, enums.SyncMode_SYNC_MODE_AUTO},
		{`""`, enums.SyncMode_SYNC_MODE_UNSPECIFIED},
		{`0`, enums.SyncMode_SYNC_MODE_UNSPECIFIED},
	}
	for _, strict := range []bool{true, false} {
		for _, tc := range tests {
			b, err := Load(writeCUE(t, syncModeCUE(tc.value)), Options{Strict: strict, Fragment: true})
			if err != nil {
				t.Errorf("sync_mode %s (strict=%v): %v", tc.value, strict, err)
				continue
			}
			if got := b.GetRules()[0].GetSyncMode(); got != tc.want {
				t.Errorf("sync_mode %s (strict=%v) = %v, want %v", tc.value, strict, got, tc.want)
			}
		}
	}
}

// What was LIVE_STATE and FULL, a typo, a number nobody defined and a value of another type are not modes. They are
// refused in a strict load and in a lenient one (which would otherwise drop the value and read AUTO), by a message
// that names the rule and lists the modes.
func TestASyncModeThatIsNotAModeIsRefused(t *testing.T) {
	for _, value := range []string{
		`"LIVE_STATE"`, `"SYNC_MODE_LIVE_STATE"`, `"FULL"`, `"SYNC_MODE_FULL"`, `"GIT_SMART_REPAR"`, `"FAST"`, `3`, `99`, `true`,
	} {
		for _, strict := range []bool{true, false} {
			_, err := Load(writeCUE(t, syncModeCUE(value)), Options{Strict: strict, Fragment: true})
			want := fmt.Sprintf("rule github/a: sync_mode %s is not a mode: use AUTO, GIT_UPSERT_ALL or GIT_SMART_REPAIR", value)
			if err == nil || !strings.Contains(err.Error(), want) {
				t.Errorf("sync_mode %s (strict=%v): error = %v, want it to say %q", value, strict, err, want)
			}
		}
	}
}

func TestEveryRuleWithABadModeIsNamedAtOnce(t *testing.T) {
	file := writeCUE(t, `bundle: rules: [
		{platform: "github", datasource: "a", sync_mode: "LIVE_STATE"},
		{platform: "github", datasource: "b", sync_mode: "AUTO"},
		{platform: "github", datasource: "c", sync_mode: "FULL"},
	]`)
	_, err := Load(file, Options{Strict: true, Fragment: true})
	if err == nil || !strings.Contains(err.Error(), "github/a") || !strings.Contains(err.Error(), "github/c") ||
		strings.Contains(err.Error(), "github/b") {
		t.Fatalf("error = %v, want github/a and github/c, not github/b", err)
	}
}

func TestTheModesAMessageListsAreTheProtosModes(t *testing.T) {
	want := map[string]bool{}
	for name := range enums.SyncMode_value {
		if name != "SYNC_MODE_UNSPECIFIED" {
			want[strings.TrimPrefix(name, "SYNC_MODE_")] = true
		}
	}
	if len(syncModes) != len(want) {
		t.Fatalf("syncModes = %v, the proto has %v", syncModes, want)
	}
	for _, mode := range syncModes {
		if !want[mode] {
			t.Errorf("%s is listed as a mode, and the proto has no such mode", mode)
		}
	}
}
