package secrets

import (
	"bufio"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"

	"go.yaml.in/yaml/v3"
)

// ReadSecretFile returns a {file: …} credential: the whole file with
// surrounding whitespace trimmed, or — when key is set — that key's value.
// With a key the file is parsed by its extension: .json, .yaml/.yml,
// otherwise KEY=value lines (.env, .properties). In JSON and YAML a dotted
// key ("db.password") reaches into nested objects.
func ReadSecretFile(path, key string) (string, error) {
	data, err := os.ReadFile(path)
	if err != nil {
		return "", fmt.Errorf("secret file: %w", err)
	}
	if key == "" {
		return strings.TrimSpace(string(data)), nil
	}

	var value interface{}
	var found bool
	switch strings.ToLower(filepath.Ext(path)) {
	case ".json":
		var doc interface{}
		if err := json.Unmarshal(data, &doc); err != nil {
			return "", fmt.Errorf("secret file %s: %w", path, err)
		}
		value, found = lookup(doc, key)
	case ".yaml", ".yml":
		var doc interface{}
		if err := yaml.Unmarshal(data, &doc); err != nil {
			return "", fmt.Errorf("secret file %s: %w", path, err)
		}
		value, found = lookup(doc, key)
	default:
		value, found = envValue(string(data), key)
	}
	if !found {
		return "", fmt.Errorf("secret file %s has no key %q", path, key)
	}
	switch v := value.(type) {
	case string:
		return v, nil
	case map[string]interface{}, []interface{}:
		return "", fmt.Errorf("secret file %s: key %q holds a %T, not a single value", path, key, v)
	case nil:
		return "", nil
	default:
		return fmt.Sprint(v), nil
	}
}

// lookup finds key in a parsed JSON/YAML document: the key as written first,
// then as a dotted path through nested objects.
func lookup(doc interface{}, key string) (interface{}, bool) {
	if m, ok := doc.(map[string]interface{}); ok {
		if v, ok := m[key]; ok {
			return v, true
		}
	}
	current := doc
	for _, part := range strings.Split(key, ".") {
		m, ok := current.(map[string]interface{})
		if !ok {
			return nil, false
		}
		if current, ok = m[part]; !ok {
			return nil, false
		}
	}
	return current, true
}

// envValue reads KEY=value lines: blank lines and # comments are skipped, an
// "export " prefix is allowed, and matching outer quotes are removed.
func envValue(content, key string) (interface{}, bool) {
	scanner := bufio.NewScanner(strings.NewReader(content))
	for scanner.Scan() {
		line := strings.TrimSpace(scanner.Text())
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		line = strings.TrimPrefix(line, "export ")
		name, value, ok := strings.Cut(line, "=")
		if !ok || strings.TrimSpace(name) != key {
			continue
		}
		value = strings.TrimSpace(value)
		if len(value) >= 2 && (value[0] == '"' || value[0] == '\'') && value[len(value)-1] == value[0] {
			if value[0] == '"' {
				if unq, err := strconv.Unquote(value); err == nil {
					return unq, true
				}
			}
			return value[1 : len(value)-1], true
		}
		return value, true
	}
	return nil, false
}
