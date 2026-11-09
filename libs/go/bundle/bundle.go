// Package bundle loads the varTrack CUE config bundle (config.cue) into the
// proto-generated Bundle message.
//
// The gateway and the watcher both read the bundle, and they read it the same
// way: evaluate the CUE, accept the shorthands the docs allow (a credential
// written as a plain string, a sync_mode by its short name), keep working with
// option names that were renamed, and then parse against the proto. That is one
// thing, so it is written once, here. The orchestrator is handed the result by Export.
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
	// config.cue stops the service at startup. Every service loads strictly (the
	// gateway, the watcher, and the orchestrator through Export): they share one
	// proto, so a field one of them does not know is a typo or a mismatched
	// release, and none of them should read a bundle the others refuse. Without
	// it unknown fields are ignored.
	Strict bool

	// Fragment says the config is a piece of a bundle, not a whole one (a snippet in the docs): the rules for
	// what a bundle must contain (a platform, a datasource, a rule) are not applied, the rules for what it does
	// contain are.
	Fragment bool
}

// Load reads the bundle in the CUE file at path.
//
// Whatever the options, a bundle Load returns has at most one rule for each
// platform and datasource: a rule is identified by that pair, and every lookup
// takes the first match, so a second one would never run. Its declared tenants agree
// with each other and with the rules (see checkTenants).
func Load(path string, opts Options) (*pb.Bundle, error) {
	jsonBytes, err := evaluate(path)
	if err != nil {
		return nil, err
	}
	return fromJSON(jsonBytes, opts)
}

// Export reads the bundle in the CUE file at path as Load does, strictly, and returns
// it as JSON: the bundle object with its shorthands expanded, its renamed options under
// their current names and its removed options gone, in the names the proto uses. This
// is what a service that does not read the proto itself (the orchestrator) is given,
// so there is one reading of config.cue and one set of rules for what it may hold: a
// bundle Export refuses, every service refuses.
func Export(path string) ([]byte, error) {
	return export(path, Options{Strict: true})
}

// export is Export with the options a test needs to load a piece of a bundle (Options.Fragment).
func export(path string, opts Options) ([]byte, error) {
	jsonBytes, err := evaluate(path)
	if err != nil {
		return nil, err
	}
	if _, err := fromJSON(jsonBytes, opts); err != nil {
		return nil, err
	}
	return jsonBytes, nil
}

// evaluate evaluates the CUE file at path and returns its bundle as JSON, normalised
// (see normalizeSecretRefs) but not yet parsed against the proto.
func evaluate(path string) ([]byte, error) {
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
	return jsonBytes, nil
}

// fromJSON reads normalised bundle JSON into the proto.
func fromJSON(jsonBytes []byte, opts Options) (*pb.Bundle, error) {
	bundle := &pb.Bundle{}
	if err := (protojson.UnmarshalOptions{DiscardUnknown: !opts.Strict}).Unmarshal(jsonBytes, bundle); err != nil {
		return nil, fmt.Errorf("failed to unmarshal into protobuf: %w", err)
	}
	if err := checkUniqueRules(bundle); err != nil {
		return nil, err
	}
	if err := checkTenants(bundle); err != nil {
		return nil, err
	}
	if err := checkVaultLogins(bundle); err != nil {
		return nil, err
	}
	if err := checkSchemaMaps(bundle); err != nil {
		return nil, err
	}
	if err := checkRules(bundle, opts.Fragment); err != nil {
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
	migrateTLSOptions(bundle)
	dropRemovedOptions(bundle)
	expandSyncModes(bundle)
	expandPruneShorthand(bundle)
	wrapSecretRefs(bundle, (&pb.Bundle{}).ProtoReflect().Descriptor())
	normalizeDurations(bundle, (&pb.Bundle{}).ProtoReflect().Descriptor())

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
