// Suite 26's secret case for an XML file: its keys start with the root element, config.
{
	"config.app.name":          string
	"config.database.host":     string
	"config.database.password": string @secret()
	...
}
