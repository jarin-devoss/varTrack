// Suite 26's secret case: database.password is a secret, fetched from Vault. A file holds a reference to it
// (@secret(secret/e2e-formats#password)), written the way its format writes a string.
{
	"app.name":          string
	"database.host":     string
	"database.password": string @secret()
	...
}
