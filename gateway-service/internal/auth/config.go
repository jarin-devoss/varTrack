package auth

import (
	"fmt"
	"strings"

	authpb "vartrack/libs/gen/vartrack/v1/models/auth"
)

// NewOPAFromConfig builds the CLI API's OPA policy from the bundle's
// auth.opa block; nil without one. A policy that does not load stops the
// gateway at startup rather than leaving the CLI API open or silently closed.
func NewOPAFromConfig(cfg *authpb.OPAConfig) (*OPAEvaluator, error) {
	if cfg == nil {
		return nil, nil
	}
	switch src := cfg.GetSource().(type) {
	case *authpb.OPAConfig_InlinePolicy:
		e, err := NewOPAEvaluatorEmbedded(src.InlinePolicy)
		if err != nil {
			return nil, fmt.Errorf("auth.opa.inline_policy: %w", err)
		}
		return e, nil
	case *authpb.OPAConfig_PolicyFile:
		e, err := NewOPAEvaluatorFromFile(src.PolicyFile)
		if err != nil {
			return nil, fmt.Errorf("auth.opa.policy_file: %w", err)
		}
		return e, nil
	case *authpb.OPAConfig_ServerUrl:
		if strings.TrimSpace(src.ServerUrl) == "" {
			return nil, fmt.Errorf("auth.opa.server_url is empty")
		}
		return NewOPAEvaluatorServer(src.ServerUrl), nil
	}
	return nil, fmt.Errorf("auth.opa needs one of inline_policy, policy_file or server_url")
}
