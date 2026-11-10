package bundle

import (
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"

	enums "vartrack/libs/gen/vartrack/v1/enums"
)

// syncModes are the modes a rule can name, in the order every message lists them.
var syncModes = []string{"AUTO", "GIT_UPSERT_ALL", "GIT_SMART_REPAIR"}

// expandSyncModes accepts the short names the docs use for a rule's
// sync_mode — "AUTO", "GIT_UPSERT_ALL", "GIT_SMART_REPAIR" — as well as the
// proto's own (SYNC_MODE_AUTO, …), in any case, or a mode's number, so a bundle
// written either way loads. Anything else is an error that names the rule and
// lists the modes: it is not read as AUTO, and not dropped by a non-strict
// parse, which would do the same. (A mode varTrack once had, LIVE_STATE or
// FULL, is not a mode.)
func expandSyncModes(bundle map[string]interface{}) error {
	var problems []error
	rules, _ := bundle["rules"].([]interface{})
	for _, r := range rules {
		rule, ok := r.(map[string]interface{})
		if !ok {
			continue
		}
		value, set := rule["sync_mode"]
		if !set || value == nil {
			continue
		}
		name, ok := syncModeName(value)
		if !ok {
			problems = append(problems, fmt.Errorf("rule %v/%v: %s", rule["platform"], rule["datasource"], syncModeProblem(value)))
			continue
		}
		rule["sync_mode"] = name
	}
	return errors.Join(problems...)
}

// syncModeName is the proto name (SYNC_MODE_AUTO) of the mode *value* spells, and whether it spells one: a
// string, short or with the SYNC_MODE_ prefix, in any case, or a number; an empty string and 0 are the unset mode.
func syncModeName(value interface{}) (string, bool) {
	var text string
	switch v := value.(type) {
	case string:
		text = strings.ToUpper(strings.TrimSpace(v))
	case json.Number:
		text = v.String()
	default:
		return "", false
	}
	if text == "" {
		return "SYNC_MODE_UNSPECIFIED", true
	}
	if n, err := strconv.ParseInt(text, 10, 32); err == nil {
		name, ok := enums.SyncMode_name[int32(n)]
		return name, ok
	}
	name := "SYNC_MODE_" + strings.TrimPrefix(text, "SYNC_MODE_")
	_, ok := enums.SyncMode_value[name]
	return name, ok
}

// syncModeProblem is the sentence for a sync_mode that is not a mode; the orchestrator says the same one.
func syncModeProblem(value interface{}) string {
	shown, _ := json.Marshal(value)
	return fmt.Sprintf("sync_mode %s is not a mode: use %s or %s",
		shown, strings.Join(syncModes[:len(syncModes)-1], ", "), syncModes[len(syncModes)-1])
}
