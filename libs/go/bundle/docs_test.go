package bundle

import (
	"fmt"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"

	"google.golang.org/protobuf/reflect/protoreflect"

	pb "vartrack/libs/gen/vartrack/v1/models"
)

// Every ```cue block in the README and the docs is loaded by the same strict loader
// the gateway starts with. A snippet the loader refuses is a setting the docs promise
// and the services reject: the docs are written against the contract, not beside it.
//
// A block that is a whole config starts with `bundle:`. A fragment is placed by its
// first key: a bundle setting (platforms, rules, ui…) goes in `bundle: { … }`, a rule
// option in a rule, a datasource, platform or secret manager kind in its list. A block
// that is deliberately not config is fenced as ```text, or marks itself ```cue notest.

const fence = "```"

var (
	fenceOpen  = regexp.MustCompile(`^(\s*)` + fence + `cue(?:\s+(\S.*))?$`)
	fenceClose = regexp.MustCompile(`^\s*` + fence + `\s*$`)
	firstKey   = regexp.MustCompile(`^\s*"?([A-Za-z_]\w*)"?\s*:`)
	leadingComments = regexp.MustCompile(`\A(?:\s*//[^\n]*\n)+`)
	hasPlat         = regexp.MustCompile(`(?m)^\s*platform:`)
	hasDS           = regexp.MustCompile(`(?m)^\s*datasource:`)
)

type snippet struct {
	where string
	text  string
}

func docSnippets(t *testing.T) []snippet {
	t.Helper()
	root := "../../.."
	files := []string{"README.md"}
	_ = filepath.Walk(filepath.Join(root, "docs"), func(p string, info os.FileInfo, err error) error {
		if err == nil && !info.IsDir() && strings.HasSuffix(p, ".md") {
			rel, _ := filepath.Rel(root, p)
			files = append(files, filepath.ToSlash(rel))
		}
		return nil
	})
	var out []snippet
	for _, f := range files {
		raw, err := os.ReadFile(filepath.Join(root, f))
		if err != nil {
			continue
		}
		lines := strings.Split(strings.ReplaceAll(string(raw), "\r\n", "\n"), "\n")
		for i := 0; i < len(lines); i++ {
			m := fenceOpen.FindStringSubmatch(lines[i])
			if m == nil {
				continue
			}
			start := i + 1
			var body []string
			for i++; i < len(lines) && !fenceClose.MatchString(lines[i]); i++ {
				body = append(body, strings.TrimPrefix(lines[i], m[1]))
			}
			if strings.Contains(m[2], "notest") {
				continue
			}
			out = append(out, snippet{fmt.Sprintf("%s:%d", f, start), strings.Join(body, "\n")})
		}
	}
	return out
}

func hasField(msg protoreflect.MessageDescriptor, name string) bool {
	return msg.Fields().ByName(protoreflect.Name(name)) != nil
}

// oneofKinds are the names of a message's oneof members ("redis", "github", …).
func oneofKinds(msg protoreflect.MessageDescriptor) map[string]bool {
	kinds := map[string]bool{}
	fields := msg.Fields()
	for i := 0; i < fields.Len(); i++ {
		if f := fields.Get(i); f.ContainingOneof() != nil && f.Message() != nil {
			kinds[string(f.Name())] = true
		}
	}
	return kinds
}

// place wraps a fragment into a whole config by what its first key is. It reports
// false for a fragment it cannot place.
func place(text string) (string, bool) {
	code := leadingComments.ReplaceAllString(text, "")
	m := firstKey.FindStringSubmatch(code)
	if m == nil {
		return "", false
	}
	first := m[1]
	bundle := (&pb.Bundle{}).ProtoReflect().Descriptor()
	rule := (&pb.Rule{}).ProtoReflect().Descriptor()
	nl := "\n"
	switch {
	case first == "bundle":
		return text, true
	case hasField(bundle, first):
		return "bundle: {" + nl + text + nl + "}" + nl, true
	case hasField(rule, first):
		head := ""
		if !hasPlat.MatchString(text) {
			head = `platform: "github"` + nl
		}
		if !hasDS.MatchString(text) {
			head += `datasource: "mongo"` + nl
		}
		return "bundle: rules: [{" + nl + head + text + nl + "}]" + nl, true
	case oneofKinds((&pb.DataSource{}).ProtoReflect().Descriptor())[first]:
		return "bundle: datasources: [{" + nl + text + nl + "}]" + nl, true
	case oneofKinds((&pb.Platform{}).ProtoReflect().Descriptor())[first]:
		return "bundle: platforms: [{" + nl + text + nl + "}]" + nl, true
	case oneofKinds((&pb.SecretManager{}).ProtoReflect().Descriptor())[first]:
		return "bundle: secret_managers: [{" + nl + text + nl + "}]" + nl, true
	}
	return "", false
}

func TestEveryConfigSnippetInTheDocsLoadsStrictly(t *testing.T) {
	snippets := docSnippets(t)
	if len(snippets) == 0 {
		t.Skip("docs not available")
	}
	for _, s := range snippets {
		text, ok := place(s.text)
		if !ok {
			t.Errorf("%s: cannot tell where this fragment sits; start it with a bundle setting, or fence it as cue notest", s.where)
			continue
		}
		// A snippet illustrates; it is held to every rule for what it contains (a Mongo without a collection
		// is wrong in the docs too), not asked to be a whole bundle. The configs the repository ships (the
		// examples, the demo's) are loaded whole, with every rule, by examples_test.go.
		if _, err := Load(writeCUE(t, text), Options{Strict: true, Fragment: true}); err != nil {
			msg := err.Error()
			if len(msg) > 1500 { // every violation matters: the list is what says what to fix
				msg = msg[:1500]
			}
			t.Errorf("%s: %s", s.where, msg)
		}
	}
}
