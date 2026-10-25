// Package bundle loads the varTrack CUE config bundle (config.cue) into the
// proto-generated Bundle message.
//
// The gateway and the watcher both read the bundle, and they read it the same
// way: evaluate the CUE, accept the shorthands the docs allow (a credential
// written as a plain string, a sync_mode by its short name), keep working with
// option names that were renamed, and then parse against the proto. That is one
// thing, so it is written once, here. The two differ in one choice, Options.Strict.
package bundle

import (
	"bytes"
	"encoding/json"
	"fmt"

	"cuelang.org/go/cue"
	"cuelang.org/go/cue/cuecontext"
	"cuelang.org/go/cue/load"
	"google.golang.org/protobuf/encoding/protojson"

	pb "vartrack/libs/gen/vartrack/v1/models"
)

// Options says how Load parses the bundle.
type Options struct {
	// Strict rejects a field the proto does not declare, so a typo in
	// config.cue stops the service at startup (the gateway). Without it unknown
	// fields are ignored, which keeps a service that reads only part of the
	// bundle working when another service's settings change (the watcher).
	Strict bool
}

// Load reads the bundle in the CUE file at path.
//
// Whatever the options, a bundle Load returns has at most one rule for each
// platform and datasource: a rule is identified by that pair, and every lookup
// takes the first match, so a second one would never run.
func Load(path string, opts Options) (*pb.Bundle, error) {
	cfg := &load.Config{}

	buildInstances := load.Instances([]string{path}, cfg)
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

	bundle := &pb.Bundle{}
	if err := (protojson.UnmarshalOptions{DiscardUnknown: !opts.Strict}).Unmarshal(jsonBytes, bundle); err != nil {
		return nil, fmt.Errorf("failed to unmarshal into protobuf: %w", err)
	}
	if err := checkUniqueRules(bundle); err != nil {
		return nil, err
	}

	return bundle, nil
}

// normalizeSecretRefs walks the JSON and converts SecretRef string shorthand
// to proto format wherever the bundle's proto declares a SecretRef field —
// platforms, datasources, the UI's sign-in credentials:
//
//	"token": "abc"  →  "token": {"value": "abc"}
//	"token": {"ref": {"path":"x", "key":"y"}}  →  unchanged (already proto format)
//
// It also moves renamed options to their current names, drops removed ones,
// and expands sync_mode's short names, so the parse that follows sees only
// what the proto declares.
func normalizeSecretRefs(data []byte) ([]byte, error) {
	var bundle map[string]interface{}
	dec := json.NewDecoder(bytes.NewReader(data))
	dec.UseNumber() // Prevent float64 casting of large integers
	if err := dec.Decode(&bundle); err != nil {
		return nil, err
	}

	normalizeUISecretRefs(bundle)
	upgradeRenamedOptions(bundle)
	dropRemovedOptions(bundle)
	expandSyncModes(bundle)
	wrapSecretRefs(bundle, (&pb.Bundle{}).ProtoReflect().Descriptor())

	return json.Marshal(bundle)
}

// normalizeUISecretRefs converts the string shorthand of the web UI's sign-in
// credentials (ui.auth.providers[].client_id / client_secret, and
// ui.auth.session_secret) to SecretRef objects. By path, not by key name:
// auth.oidc.client_id elsewhere in the bundle is a plain string.
func normalizeUISecretRefs(bundle map[string]interface{}) {
	ui, _ := bundle["ui"].(map[string]interface{})
	auth, _ := ui["auth"].(map[string]interface{})
	if auth == nil {
		return
	}
	wrap := func(m map[string]interface{}, key string) {
		if s, ok := m[key].(string); ok {
			m[key] = map[string]interface{}{"value": s}
		}
	}
	wrap(auth, "session_secret")
	providers, _ := auth["providers"].([]interface{})
	for _, p := range providers {
		if provider, ok := p.(map[string]interface{}); ok {
			wrap(provider, "client_id")
			wrap(provider, "client_secret")
		}
	}
}
