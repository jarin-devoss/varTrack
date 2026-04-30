# Example config file — HCL (Terraform/HashiCorp) format.
# VarTrack parses .hcl and .tfvars files using python-hcl2.

app_name    = "my-service"
app_version = "1.0.0"
environment = "production"
log_level   = "info"

server {
  host    = "0.0.0.0"
  port    = 8080
  workers = 4
  timeout = "30s"
}

database {
  host     = "prod-db.internal"
  port     = 5432
  name     = "myapp"
  user     = "app-user"
  # Vault @secret annotation — resolved at ETL time.
  password = "@secret(secret/myapp/db#password)"
  pool_max = 20
  ssl      = true
}

redis {
  host = "redis.internal"
  port = 6379
  db   = 0
}

feature_flags = {
  new_ui   = true
  beta_api = false
}
