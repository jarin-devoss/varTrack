package driver

// ResolveTagName builds a name from a driver type and an optional tag.
//
//	ResolveTagName("github", "dr")  → "github-dr"
//	ResolveTagName("github", "")    → "github"
func ResolveTagName(typeName, tag string) string {
	if tag != "" {
		return typeName + "-" + tag
	}
	return typeName
}
