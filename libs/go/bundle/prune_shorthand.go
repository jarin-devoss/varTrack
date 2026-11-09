package bundle

// expandPruneShorthand accepts `prune: true` and `prune: false`, the form the docs lead
// with, as the structured block the proto declares (`prune: {enabled: true}`), as the
// proto's own comment says they are equivalent. Without it the strict parse refused the
// most common way to write the option.
func expandPruneShorthand(bundle map[string]interface{}) {
	rules, _ := bundle["rules"].([]interface{})
	for _, r := range rules {
		rule, ok := r.(map[string]interface{})
		if !ok {
			continue
		}
		if enabled, isBool := rule["prune"].(bool); isBool {
			rule["prune"] = map[string]interface{}{"enabled": enabled}
		}
	}
}
