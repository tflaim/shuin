#!/bin/sh
set -eu

CDPATH= cd "$(dirname "$0")/.."
PYTHON=${PYTHON:-python3}
VERIFY_OUT=${VERIFY_OUT:-$(mktemp -d)}
VERIFY_PYTEST_CMD=${VERIFY_PYTEST_CMD:-'"$PYTHON" -m pytest -q'}
mkdir -p "$VERIFY_OUT"
VERIFY_OUT=$(CDPATH= cd "$VERIFY_OUT" && pwd -P)
export PYTHON TMPDIR="$VERIFY_OUT" PYTHONDONTWRITEBYTECODE=1
export PYTEST_ADDOPTS="${PYTEST_ADDOPTS:-} -p no:cacheprovider"

run_suite() {
    suite=$1
    tmp=$(mktemp -d "$VERIFY_OUT/$suite.XXXXXX") || exit 1
    TMPDIR=$tmp SHUIN_HOME="$tmp/home" SHUIN_DB="$tmp/home/ledger.db" \
        sh -c "$2" > "$VERIFY_OUT/$suite.log" 2>&1
}

head_sha=$(git rev-parse HEAD)
python_version=$("$PYTHON" --version 2>&1 | sed -n '1p')
python_json=$("$PYTHON" -c 'import json, sys; print(json.dumps(sys.argv[1]))' "$python_version")

if run_suite pytest "$VERIFY_PYTEST_CMD"; then
    pytest_exit=0
else
    pytest_exit=$?
fi
if run_suite githooks "${VERIFY_HOOKS_CMD:-sh githooks/test.sh}"; then
    hooks_exit=0
else
    hooks_exit=$?
fi

set -- $(awk '
    /[^[:space:]]/ { last = $0 }
    END {
        passed = failed = "null"
        gsub(/[,=]/, " ", last)
        n = split(last, words)
        for (i = 2; i <= n; i++) {
            if (words[i-1] !~ /^[0-9]+$/) continue
            if (words[i] == "passed") passed = words[i-1] + 0
            if (words[i] == "failed") failed = words[i-1] + 0
        }
        if (passed != "null" && failed == "null") failed = 0
        print passed, failed
    }
' "$VERIFY_OUT/pytest.log")
pytest_passed=$1 pytest_failed=$2
set -- $(awk '
    BEGIN { passed = failed = "null" }
    /^githooks: [0-9]+ passed, [0-9]+ failed$/ {
        passed = $2 + 0
        failed = $4 + 0
    }
    END { print passed, failed }
' "$VERIFY_OUT/githooks.log")
hooks_passed=$1 hooks_failed=$2

ok=false
if [ "$pytest_exit" -eq 0 ] && [ "$hooks_exit" -eq 0 ] &&
    [ "$pytest_failed" = 0 ] && [ "$hooks_failed" = 0 ] &&
    [ "$pytest_passed" != null ] && [ "$hooks_passed" != null ]; then
    ok=true
fi
printf '{"head_sha":"%s","python":%s,"pytest":{"passed":%s,"failed":%s,"exit":%s},"githooks":{"passed":%s,"failed":%s,"exit":%s},"ok":%s}\n' \
    "$head_sha" "$python_json" "$pytest_passed" "$pytest_failed" "$pytest_exit" \
    "$hooks_passed" "$hooks_failed" "$hooks_exit" "$ok" > "$VERIFY_OUT/verify.json"
printf '%s\n' "$VERIFY_OUT/verify.json"
[ "$ok" = true ]
