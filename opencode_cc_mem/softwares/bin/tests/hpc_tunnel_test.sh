#!/usr/bin/env bash
# Unit tests for hpc_tunnel.sh — uses PATH-stubbing to inject fake `pgrep`,
# `pass`, `sudo`, `ss` without actually starting openconnect.
#
# Run all tests: ./hpc_tunnel_test.sh
# Run one test:  ./hpc_tunnel_test.sh test_already_running
set -uo pipefail

SCRIPT="$(cd "$(dirname "$0")"/.. && pwd)/hpc_tunnel.sh"
test -x "$SCRIPT" || { echo "FAIL: $SCRIPT not executable"; exit 1; }

PASS_COUNT=0
FAIL_COUNT=0

# Make a temporary PATH-stub directory. Returns its path on stdout.
make_stub_dir() {
    mktemp -d
}

# Write an executable stub of `name` in $1 that prints $4 and exits with $3.
write_stub() {
    local dir="$1" name="$2" exit_code="$3" stdout="${4:-}"
    cat > "$dir/$name" <<EOF
#!/bin/bash
[ -n "$stdout" ] && printf '%s\n' "$stdout"
exit $exit_code
EOF
    chmod +x "$dir/$name"
}

assert_exit() {
    local got="$1" want="$2" name="$3"
    if [ "$got" -eq "$want" ]; then
        PASS_COUNT=$((PASS_COUNT + 1))
        echo "PASS: $name (exit $got)"
    else
        FAIL_COUNT=$((FAIL_COUNT + 1))
        echo "FAIL: $name (got exit $got, want $want)"
    fi
}

# ---- tests ----

test_already_running() {
    # pgrep returns a match → script should exit 0 fast.
    local dir; dir=$(make_stub_dir)
    write_stub "$dir" pgrep 0 "12345 openconnect"
    write_stub "$dir" ss 1 ""           # not reached
    write_stub "$dir" pass 1 ""         # not reached
    write_stub "$dir" sudo 1 ""         # not reached
    PATH="$dir:$PATH" bash "$SCRIPT" >/dev/null 2>&1
    assert_exit $? 0 "already_running"
    rm -rf "$dir"
}

test_port_already_listening() {
    # pgrep finds nothing, but :1080 is listening (e.g. set up by another tool)
    local dir; dir=$(make_stub_dir)
    write_stub "$dir" pgrep 1 ""
    write_stub "$dir" ss 0 "LISTEN 0 128 127.0.0.1:1080 0.0.0.0:*"
    write_stub "$dir" pass 1 ""         # not reached
    write_stub "$dir" sudo 1 ""         # not reached
    PATH="$dir:$PATH" bash "$SCRIPT" >/dev/null 2>&1
    assert_exit $? 0 "port_already_listening"
    rm -rf "$dir"
}

test_missing_pass_entry() {
    # No tunnel/port, but `pass show` fails → exit 2.
    local dir; dir=$(make_stub_dir)
    write_stub "$dir" pgrep 1 ""
    write_stub "$dir" ss 1 ""
    write_stub "$dir" pass 1 ""         # `pass show ...` returns non-zero
    write_stub "$dir" sudo 0 ""
    PATH="$dir:$PATH" bash "$SCRIPT" >/dev/null 2>&1
    assert_exit $? 2 "missing_pass_entry"
    rm -rf "$dir"
}

test_missing_sudoers_rule() {
    # pass works, but `sudo -n true` fails → exit 3.
    local dir; dir=$(make_stub_dir)
    write_stub "$dir" pgrep 1 ""
    write_stub "$dir" ss 1 ""
    write_stub "$dir" pass 0 "secret"
    write_stub "$dir" sudo 1 ""         # `sudo -n` fails
    PATH="$dir:$PATH" bash "$SCRIPT" >/dev/null 2>&1
    assert_exit $? 3 "missing_sudoers_rule"
    rm -rf "$dir"
}

# Note: cold-start happy path is integration-tested manually (separate task) —
# can't be unit-tested without actually spawning openconnect.

# Allow running a single test by name.
if [ $# -gt 0 ]; then
    "$1"
else
    test_already_running
    test_port_already_listening
    test_missing_pass_entry
    test_missing_sudoers_rule
fi

echo ""
echo "Total: $PASS_COUNT passed, $FAIL_COUNT failed"
[ "$FAIL_COUNT" -eq 0 ]
