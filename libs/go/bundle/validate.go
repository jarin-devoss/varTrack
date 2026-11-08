package bundle

import (
	"errors"
	"fmt"
	"strings"
	"sync"

	validate "buf.build/gen/go/bufbuild/protovalidate/protocolbuffers/go/buf/validate"
	"buf.build/go/protovalidate"
	"google.golang.org/genproto/googleapis/api/annotations"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"

	pb "vartrack/libs/gen/vartrack/v1/models"
)

// The protos declare their own rules (buf.validate): a Redis database is 0 to 15, a Mongo username comes with
// its password, a Mongo needs a collection unless it takes one per environment. The loader parses strictly,
// which catches a field the proto does not have; this catches a field whose value the proto says is wrong, so a
// config that breaks a rule is refused when it loads, in every service, instead of when the first push reaches
// the sink that needed it.
var (
	rulesOnce sync.Once
	rules     protovalidate.Validator
	rulesErr  error
)

// checkRules runs the rules the protos declare over the parsed bundle. All the violations come back in one
// error, each with the path of the field it is about (datasources[3].redis.database).
//
// A rule on a field applies when the config sets that field, or when the proto says the field has to be there
// (buf.validate required, a minimum number of items, google.api.field_behavior REQUIRED). A field the config
// leaves out is not checked against its rules: the services read an unset field as its default, and several
// protos state that default as a rule (a Gitea platform's git_scm_signature must equal X-Gitea-Signature, a
// Redis pool is 1 to 1000), which an unset field would break. With fragment the config is a piece of a bundle
// (a snippet in the docs): nothing is demanded of what it leaves out, a platform included.
func checkRules(bundle *pb.Bundle, fragment bool) error {
	rulesOnce.Do(func() { rules, rulesErr = protovalidate.New() })
	if rulesErr != nil {
		return fmt.Errorf("the protos' validation rules cannot be loaded: %w", rulesErr)
	}
	err := rules.Validate(bundle, protovalidate.WithFilter(setOrRequired(fragment)))
	if err == nil {
		return nil
	}
	var violated *protovalidate.ValidationError
	if !errors.As(err, &violated) {
		return fmt.Errorf("the config cannot be checked against the protos' validation rules: %w", err)
	}
	lines := make([]string, 0, len(violated.Violations))
	for _, violation := range violated.Violations {
		lines = append(lines, "  - "+violation.String())
	}
	plural := ""
	if len(lines) != 1 {
		plural = "s"
	}
	return fmt.Errorf("the config breaks %d rule%s its protos declare:\n%s", len(lines), plural, strings.Join(lines, "\n"))
}

// ruleMessage is the message a rule's own rules are written for: the *merged* rule. config.cue holds only its
// first layer (the tenant's bundle.json supplies file_name, a repository's vartrack.json the rest), so a rule
// such as "give file_name or file_path_map" cannot be asked of a layer; it is checked where the layers meet.
var ruleMessage = (&pb.Rule{}).ProtoReflect().Descriptor().FullName()

func setOrRequired(fragment bool) protovalidate.Filter {
	return protovalidate.FilterFunc(func(msg protoreflect.Message, descriptor protoreflect.Descriptor) bool {
		switch d := descriptor.(type) {
		case protoreflect.MessageDescriptor:
			return d.FullName() != ruleMessage // a message's own rules apply wherever it is, except the rule's
		case protoreflect.FieldDescriptor:
			return msg.Has(d) || (!fragment && mustBeThere(d))
		}
		return true // a oneof's own rules
	})
}

// mustBeThere says whether the proto demands the field, set or not.
func mustBeThere(field protoreflect.FieldDescriptor) bool {
	options := field.Options()
	if rules, ok := proto.GetExtension(options, validate.E_Field).(*validate.FieldRules); ok && rules != nil {
		if rules.GetRequired() || rules.GetRepeated().GetMinItems() > 0 || rules.GetMap().GetMinPairs() > 0 {
			return true
		}
	}
	if behaviours, ok := proto.GetExtension(options, annotations.E_FieldBehavior).([]annotations.FieldBehavior); ok {
		for _, behaviour := range behaviours {
			if behaviour == annotations.FieldBehavior_REQUIRED {
				return true
			}
		}
	}
	return false
}
