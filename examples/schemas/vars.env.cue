// A schema for examples/configs/vars.env — a .env file.
//
// Every value of a .env file is text: SERVER_PORT=8080 is the text "8080". The schema still says `int`: a text value
// that reads as a whole number (no leading zero, no sign but "-") is checked as one, so ranges work. `bool` takes
// true or false in any case, and nothing else (yes, no, on, off, 1 and 0 are text).
//
// Place it in the schema repository as `vars.env.cue`: a file is checked by the schema named after it.

APP_NAME:    string & !=""
APP_VERSION: =~"^[0-9]+\\.[0-9]+\\.[0-9]+$"
APP_ENV:     "production" | "staging" | "development"
LOG_LEVEL:   "debug" | "info" | "warn" | "error"
DEBUG:       bool

SERVER_HOST:    string
SERVER_PORT:    int & >=1024 & <=65535
SERVER_WORKERS: int & >=1 & <=64
SERVER_TIMEOUT: int & >=1 & <=300

DB_HOST:      string & !=""
DB_PORT:      int & >0
DB_NAME:      string & !=""
DB_USER:      string & !=""
DB_PASSWORD:  string & !="" @secret()
DB_POOL_SIZE: int & >=1 & <=100
DB_SSL:       bool

REDIS_HOST: string
REDIS_PORT: int & >0
REDIS_DB:   int & >=0

JWT_ALGORITHM: "HS256" | "HS384" | "HS512"
JWT_EXPIRY:    int & >0
JWT_SECRET:    string & !="" @secret()

FEATURE_NEW_UI:   bool
FEATURE_BETA_API: bool
