package models

import (
	"gateway-service/internal/driver"

	pb_models "gateway-service/internal/gen/proto/go/vartrack/v1/models"
)

// DataSourceName returns the resolved name for a datasource.
// If a tag is set, the name is "{type}-{tag}" (e.g. "mongo-analytics").
// Otherwise, it falls back to the type name (e.g. "mongo").
func DataSourceName(ds *pb_models.DataSource) string {
	switch config := ds.Config.(type) {
	case *pb_models.DataSource_Mongo:
		return driver.ResolveTagName("mongo", config.Mongo.GetTag())
	default:
		return ""
	}
}
