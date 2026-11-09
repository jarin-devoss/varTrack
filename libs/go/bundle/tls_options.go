package bundle

import (
	"fmt"
	"log"
	"sort"
	"strings"
)

// The TLS settings of the platforms, the Vault secret manager and S3 were written as
// `verify_ssl`, a switch whose unset value was "false": the Go services then did not
// check the server's certificate at all, while the Python ones did. The setting is now
// `insecure_skip_verify`, named for the unsafe choice so that leaving it out is safe.
// A config written with the old name keeps working, and its meaning is carried over:
// `verify_ssl: false` is `insecure_skip_verify: true`, and `verify_ssl: true` is the
// default and says nothing.
//
// Vault's `ssl_ca` is the CA's content (PEM). A value that is not PEM can only have been
// a file name, which is what the Python reader took it for, so it moves to `ssl_ca_file`.
// A field never means both.
//
// This is a change of meaning, not a rename, so it is not in the protos' old_names.

const pemMarker = "-----BEGIN"

// migrateTLSOptions rewrites the old TLS spellings in place, before the bundle is
// parsed against the proto. It returns what it changed.
func migrateTLSOptions(bundle map[string]interface{}) []string {
	var moved []string

	verify := func(m map[string]interface{}, where string) {
		old, ok := m["verify_ssl"]
		if !ok {
			return
		}
		delete(m, "verify_ssl")
		if checked, isBool := old.(bool); isBool && !checked {
			if _, set := m["insecure_skip_verify"]; !set {
				m["insecure_skip_verify"] = true
			}
			moved = append(moved, fmt.Sprintf("%s.verify_ssl: false is now insecure_skip_verify: true", where))
			return
		}
		moved = append(moved, fmt.Sprintf("%s.verify_ssl is now insecure_skip_verify, which is off unless set", where))
	}

	each := func(key, kind string, fn func(map[string]interface{}, string)) {
		list, _ := bundle[key].([]interface{})
		for i, item := range list {
			wrapper, _ := item.(map[string]interface{})
			if cfg, ok := wrapper[kind].(map[string]interface{}); ok {
				fn(cfg, fmt.Sprintf("%s[%d].%s", key, i, kind))
			}
		}
	}

	each("platforms", "github", verify)
	each("platforms", "gitea", verify)
	each("datasources", "s3", verify)
	each("secret_managers", "vault", func(m map[string]interface{}, where string) {
		verify(m, where)
		ca, isString := m["ssl_ca"].(string)
		if !isString || ca == "" || strings.Contains(ca, pemMarker) {
			return
		}
		delete(m, "ssl_ca")
		if _, set := m["ssl_ca_file"]; !set {
			m["ssl_ca_file"] = ca
		}
		moved = append(moved, fmt.Sprintf("%s.ssl_ca is a file name, not PEM content: it is now ssl_ca_file", where))
	})

	sort.Strings(moved)
	for _, m := range moved {
		log.Printf("bundle: %s; the old form still works", m)
	}
	return moved
}
