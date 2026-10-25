package bundle

import (
	"google.golang.org/protobuf/reflect/protoreflect"
)

// secretRefName is the proto message every credential field uses.
const secretRefName protoreflect.FullName = "vartrack.v1.SecretRef"

// wrapSecretRefs converts the string shorthand of every SecretRef field in
// v — a JSON object decoded from the bundle — to a SecretRef object:
//
//	"password": "x"  →  "password": {"value": "x"}
//
// It follows md, the proto message v decodes into, so only fields the proto
// declares as SecretRef are touched: a MongoDB "password" or a Vault
// "token", which are plain strings in their protos, stay as they are. The
// object forms ({value}, {file}, {ref}) pass through unchanged.
func wrapSecretRefs(v interface{}, md protoreflect.MessageDescriptor) {
	obj, ok := v.(map[string]interface{})
	if !ok {
		return
	}
	for key, child := range obj {
		fd := md.Fields().ByName(protoreflect.Name(key))
		if fd == nil {
			fd = md.Fields().ByJSONName(key)
		}
		if fd == nil {
			continue
		}
		switch {
		case fd.IsMap():
			if fd.MapValue().Kind() != protoreflect.MessageKind {
				continue
			}
			if entries, ok := child.(map[string]interface{}); ok {
				for k, e := range entries {
					entries[k] = wrapValue(e, fd.MapValue().Message())
				}
			}
		case fd.Kind() != protoreflect.MessageKind:
			continue
		case fd.IsList():
			if items, ok := child.([]interface{}); ok {
				for i, e := range items {
					items[i] = wrapValue(e, fd.Message())
				}
			}
		default:
			obj[key] = wrapValue(child, fd.Message())
		}
	}
}

// wrapValue returns v as a SecretRef object when md is SecretRef and v is
// its string shorthand; otherwise it descends into v and returns it.
func wrapValue(v interface{}, md protoreflect.MessageDescriptor) interface{} {
	if md.FullName() == secretRefName {
		if s, ok := v.(string); ok {
			return map[string]interface{}{"value": s}
		}
		return v
	}
	wrapSecretRefs(v, md)
	return v
}
