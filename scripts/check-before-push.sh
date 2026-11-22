#!/bin/sh
# Runs, before a push, the checks CI runs that compare a generated or locked file with its source, and then the tests CI
# runs first. It checks what is COMMITTED (HEAD, or --ref), in a scratch copy of the checkout, so it changes nothing in
# yours and a file you forgot to commit is caught.
#
#   sh scripts/check-before-push.sh               every check
#   sh scripts/check-before-push.sh --quick       the three consistency checks, without the tests
#   sh scripts/check-before-push.sh --only lock   one check: generated | lock | tidy | tests
#   sh scripts/check-before-push.sh --ref <rev>   another commit than HEAD
#   sh scripts/check-before-push.sh --here        in this checkout, not a scratch copy (CI does: its checkout is disposable)
#
# The checks, each the same one CI runs:
#   generated  buf generate, then libs/go/gen must not change      (.github/workflows/ci.yml, job libs/go)
#   lock       npm ci in ui/ into a clean node_modules             (a package.json that names a package its lock lacks)
#   tidy       go mod tidy, then go.mod / go.sum must not change   (jobs libs/go, gateway-service, watcher-service, orchestrator-service: tools/cue)
#   tests      ruff and pytest (orchestrator), go vet and go test (libs/go), tsc and vitest (ui)
#
# Exit status: 0 every check passed, 1 a check failed (it says which file, and the command that fixes it), 2 a tool is missing.
set -eu

only=""
ref="HEAD"
here=0
quick=0
while [ $# -gt 0 ]; do
    case "$1" in
        --quick) quick=1 ;;
        --here) here=1 ;;
        --only) shift; only="${1:-}" ;;
        --ref) shift; ref="${1:-}" ;;
        -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown argument: $1 (see --help)" >&2; exit 2 ;;
    esac
    shift
done
case "$only" in ""|generated|lock|tidy|tests) ;; *) echo "--only takes generated, lock, tidy or tests" >&2; exit 2 ;; esac

want() {  # is this check on the list?
    if [ -n "$only" ]; then [ "$only" = "$1" ]; return; fi
    if [ "$1" = tests ] && [ "$quick" = 1 ]; then return 1; fi
    return 0
}

need() {  # need <command> <where to get it>
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "$1 is not installed ($2), so this check cannot run." >&2
        exit 2
    fi
}

fail() { echo "FAIL  $*" >&2; exit 1; }

root=$(git rev-parse --show-toplevel)
scratch=""
if [ "$here" = 1 ]; then
    tree="$root"
else
    need git "https://git-scm.com"
    scratch=$(mktemp -d)
    trap 'git -C "$root" worktree remove --force "$scratch/tree" >/dev/null 2>&1 || true; rm -rf "$scratch"' EXIT INT TERM
    git -C "$root" worktree add --detach "$scratch/tree" "$ref" >/dev/null 2>&1 || fail "cannot check out $ref"
    tree="$scratch/tree"
fi
echo "checking $(git -C "$tree" rev-parse --short HEAD): $(git -C "$tree" log -1 --format=%s | cut -c1-80)"

stubs_done=0
generate() {  # buf generate once per run
    [ "$stubs_done" = 1 ] && return
    need buf "https://buf.build/docs/installation"
    (cd "$tree" && buf generate) || fail "buf generate failed"
    stubs_done=1
}

check_generated() {
    echo "== generated code: buf generate, then libs/go/gen must not change"
    generate
    if [ -n "$(git -C "$tree" status --porcelain -- libs/go/gen)" ]; then
        git -C "$tree" status --short -- libs/go/gen
        git -C "$tree" diff --stat -- libs/go/gen
        fail "libs/go/gen is out of date: run buf generate and commit the result"
    fi
    echo "ok"
}

check_lock() {
    echo "== lock file: npm ci in ui/ into a clean node_modules"
    need npm "https://nodejs.org"
    if ! out=$(cd "$tree/ui" && npm ci --no-audit --no-fund --ignore-scripts 2>&1); then
        echo "$out" | grep -E "Missing|Invalid|EUSAGE|ERR" | head -n 12 >&2 || echo "$out" | tail -n 8 >&2
        fail "ui/package.json and ui/package-lock.json disagree: run npm install in ui/ and commit package-lock.json"
    fi
    echo "ok"
}

check_tidy() {
    echo "== go modules: go mod tidy must not change go.mod or go.sum"
    need go "https://go.dev/dl"
    for m in libs/go:"go.mod go.sum" gateway-service:"go.sum" watcher-service:"go.sum" tools/cue:"go.mod go.sum"; do
        dir=${m%%:*}; files=${m#*:}
        (cd "$tree/$dir" && go mod tidy) || fail "go mod tidy failed in $dir"
        # shellcheck disable=SC2086
        if ! git -C "$tree" diff --quiet -- $(for f in $files; do printf '%s/%s ' "$dir" "$f"; done); then
            # shellcheck disable=SC2086
            git -C "$tree" diff --stat -- $(for f in $files; do printf '%s/%s ' "$dir" "$f"; done)
            fail "$dir is not tidy: run go mod tidy in $dir and commit $files"
        fi
    done
    echo "ok"
}

check_tests() {
    echo "== tests: ruff and pytest, go vet and go test, tsc and vitest"
    generate  # the orchestrator imports the generated Python stubs, which are not committed
    need python "https://www.python.org"
    need go "https://go.dev/dl"
    need npm "https://nodejs.org"
    (cd "$tree/orchestrator-service" && python -m ruff check . && python -m pytest tests/unit -q -p no:cacheprovider) \
        || fail "the orchestrator's ruff or unit tests failed (pip install -e \"orchestrator-service[test]\" if a module is missing)"
    (cd "$tree/libs/go" && go vet ./... && go test ./... -timeout 120s) || fail "libs/go: go vet or go test failed"
    [ -d "$tree/ui/node_modules" ] || (cd "$tree/ui" && npm ci --no-audit --no-fund --ignore-scripts >/dev/null 2>&1) \
        || fail "npm ci failed in ui/ (run --only lock)"
    (cd "$tree/ui" && npx tsc --noEmit && npx tsc --noEmit -p e2e/tsconfig.json && npx vitest run) \
        || fail "ui: tsc or vitest failed"
    echo "ok"
}

start=$(date +%s)
want generated && check_generated
want lock && check_lock
want tidy && check_tidy
want tests && check_tests
echo "all checks passed in $(( $(date +%s) - start )) s"
