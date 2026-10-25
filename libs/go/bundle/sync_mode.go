package bundle

import "strings"

// expandSyncModes accepts the short names the docs use for a rule's
// sync_mode — "AUTO", "GIT_UPSERT_ALL", "GIT_SMART_REPAIR", "LIVE_STATE" —
// as well as the proto's own (SYNC_MODE_AUTO, …), so a bundle written either
// way loads. "FULL" and SYNC_MODE_FULL, older names for a full upsert, read
// as GIT_UPSERT_ALL, as they do in the orchestrator.
func expandSyncModes(bundle map[string]interface{}) {
	rules, _ := bundle["rules"].([]interface{})
	for _, r := range rules {
		rule, ok := r.(map[string]interface{})
		if !ok {
			continue
		}
		mode, ok := rule["sync_mode"].(string)
		if !ok || mode == "" {
			continue
		}
		short := strings.TrimPrefix(strings.ToUpper(mode), "SYNC_MODE_")
		if short == "FULL" {
			short = "GIT_UPSERT_ALL"
		}
		rule["sync_mode"] = "SYNC_MODE_" + short
	}
}
