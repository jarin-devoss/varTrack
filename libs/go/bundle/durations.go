package bundle

import (
	"strconv"
	"time"

	"google.golang.org/protobuf/reflect/protoreflect"
)

// durationName is the proto message every duration field uses.
const durationName protoreflect.FullName = "google.protobuf.Duration"

// normalizeDurations rewrites every duration the way the docs write it ("5m", "1h30m",
// "250ms") into the only form the proto's JSON takes, seconds with an "s" ("300s",
// "5400s", "0.25s"), so a bundle written either way loads. Like wrapSecretRefs it follows
// the proto, so only fields declared as durations are touched. A value that is not a
// duration is left for the parse to refuse, which names the field.
func normalizeDurations(v interface{}, md protoreflect.MessageDescriptor) {
	obj, ok := v.(map[string]interface{})
	if !ok {
		return
	}
	for key, child := range obj {
		fd := md.Fields().ByName(protoreflect.Name(key))
		if fd == nil {
			fd = md.Fields().ByJSONName(key)
		}
		if fd == nil || fd.Kind() != protoreflect.MessageKind && !fd.IsMap() {
			continue
		}
		switch {
		case fd.IsMap():
			if fd.MapValue().Kind() != protoreflect.MessageKind {
				continue
			}
			if entries, ok := child.(map[string]interface{}); ok {
				for k, e := range entries {
					entries[k] = durationValue(e, fd.MapValue().Message())
				}
			}
		case fd.IsList():
			if items, ok := child.([]interface{}); ok {
				for i, e := range items {
					items[i] = durationValue(e, fd.Message())
				}
			}
		default:
			obj[key] = durationValue(child, fd.Message())
		}
	}
}

func durationValue(v interface{}, md protoreflect.MessageDescriptor) interface{} {
	if md.FullName() == durationName {
		if s, ok := v.(string); ok {
			if d, err := time.ParseDuration(s); err == nil {
				return strconv.FormatFloat(d.Seconds(), 'f', -1, 64) + "s"
			}
		}
		return v
	}
	normalizeDurations(v, md)
	return v
}
