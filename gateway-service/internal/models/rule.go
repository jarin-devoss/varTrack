package models

import pb "gateway-service/internal/gen/proto/go/vartrack/v1/models"

// Rule wraps a protobuf Rule with convenience methods for
// resolving combined inclusion/exclusion repository patterns.
type Rule struct {
	*pb.Rule
}

// InclusionPatterns returns the union of base repository patterns and
// all enabled override match patterns.
func (r *Rule) InclusionPatterns() []string {
	patterns := append([]string{}, r.GetRepositories()...)
	for _, override := range r.GetOverrides() {
		if override.GetEnable() {
			patterns = append(patterns, override.GetMatchRepositories()...)
		}
	}
	return patterns
}

// ExclusionPatterns returns the union of base exclusions and
// all enabled override exclusion patterns.
func (r *Rule) ExclusionPatterns() []string {
	patterns := append([]string{}, r.GetExcludeRepositories()...)
	for _, override := range r.GetOverrides() {
		if override.GetEnable() {
			patterns = append(patterns, override.GetExcludeRepositories()...)
		}
	}
	return patterns
}
