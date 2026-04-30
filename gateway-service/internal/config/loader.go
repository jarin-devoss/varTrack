package config

import (
	"bytes"
	"encoding/json"
	"fmt"

	"gateway-service/internal/models"

	pb_models "gateway-service/internal/gen/proto/go/vartrack/v1/models"

	"cuelang.org/go/cue"
	"cuelang.org/go/cue/cuecontext"
	"cuelang.org/go/cue/load"
	"google.golang.org/protobuf/encoding/protojson"

	// Register platform drivers.
	_ "gateway-service/internal/models/platforms"
	// Register secret manager drivers.
	_ "gateway-service/internal/models/secretmanagers"
)

func NewBundle(cuePath string) (*models.Bundle, error) {
	bundle, err := loadBundleFromCueFile(cuePath)
	if err != nil {
		return nil, fmt.Errorf("failed to load bundle from CUE: %w", err)
	}

	return models.NewBundle(bundle), nil
}

func loadBundleFromCueFile(cuePath string) (*pb_models.Bundle, error) {
	cfg := &load.Config{}

	buildInstances := load.Instances([]string{cuePath}, cfg)
	if len(buildInstances) == 0 {
		return nil, fmt.Errorf("no CUE instances found")
	}

	if buildInstances[0].Err != nil {
		return nil, fmt.Errorf("failed to load CUE files: %w", buildInstances[0].Err)
	}

	ctx := cuecontext.New()

	value := ctx.BuildInstance(buildInstances[0])
	if value.Err() != nil {
		return nil, fmt.Errorf("failed to build CUE: %w", value.Err())
	}

	bundleValue := value.LookupPath(cue.ParsePath("bundle"))
	if bundleValue.Err() != nil {
		return nil, fmt.Errorf("bundle not found in CUE: %w", bundleValue.Err())
	}

	if err := bundleValue.Validate(cue.Concrete(true)); err != nil {
		return nil, fmt.Errorf("bundle validation failed: %w", err)
	}

	jsonBytes, err := bundleValue.MarshalJSON()
	if err != nil {
		return nil, fmt.Errorf("failed to marshal bundle to JSON: %w", err)
	}

	// Normalize SecretRef shorthand before protojson unmarshal.
	// Converts: "token": "abc" → "token": {"value": "abc"}
	// Converts: "token": {"path":"x","key":"y"} → "token": {"ref":{"path":"x","key":"y"}}
	jsonBytes, err = normalizeSecretRefs(jsonBytes)
	if err != nil {
		return nil, fmt.Errorf("failed to normalize secret refs: %w", err)
	}

	bundle := &pb_models.Bundle{}
	if err := protojson.Unmarshal(jsonBytes, bundle); err != nil {
		return nil, fmt.Errorf("failed to unmarshal into protobuf: %w", err)
	}

	return bundle, nil
}

// secretRefFields lists the platform field names that are SecretRef types.
var secretRefFields = map[string]bool{
	"token":    true,
	"password": true,
	"secret":   true,
}

// normalizeSecretRefs walks the JSON and converts SecretRef string shorthand to proto format.
// For each platform in "platforms", it checks the SecretRef fields:
//
//	"token": "abc"  →  "token": {"value": "abc"}
//	"token": {"ref": {"path":"x", "key":"y"}}  →  unchanged (already proto format)
func normalizeSecretRefs(data []byte) ([]byte, error) {
	var bundle map[string]interface{}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber() // Prevent float64 casting of large integers
	if err := dec.Decode(&bundle); err != nil {
		return nil, err
	}

	platforms, ok := bundle["platforms"].([]interface{})
	if !ok {
		return data, nil
	}

	for _, p := range platforms {
		platformWrapper, ok := p.(map[string]interface{})
		if !ok {
			continue
		}
		for _, platformConfig := range platformWrapper {
			config, ok := platformConfig.(map[string]interface{})
			if !ok {
				continue
			}
			normalizeFieldsRecursive(config)
		}
	}

	return json.Marshal(bundle)
}

// normalizeFieldsRecursive recursively traverses maps and slices
// to convert string SecretRefs into their structured equivalents.
func normalizeFieldsRecursive(v interface{}) {
	switch val := v.(type) {
	case map[string]interface{}:
		for k, child := range val {
			if secretRefFields[k] {
				// Base case: it's a known secret field.
				if str, ok := child.(string); ok {
					val[k] = map[string]interface{}{
						"value": str,
					}
				}
				// If it's already an object (like {"ref": ...}), it passes through.
			} else {
				// Recurse down.
				normalizeFieldsRecursive(child)
			}
		}
	case []interface{}:
		for _, child := range val {
			normalizeFieldsRecursive(child)
		}
	}
}
