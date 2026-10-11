#!/bin/sh
# check-listen-only.sh -- the lock-screen extension must be LISTEN-ONLY.
#
# It subscribes to the daemon's ScanState signal and calls NOTHING: a
# method call from the lock screen is a camera activation or an
# animation trigger behind the user's back. If this check fails, a
# future change has silently reopened a call path from the lock screen.
#
# dbusClient.js must contain no proxy method invocations (no ".call(")
# and no <method> entries in its introspection XML -- the ScanState
# <signal> is the only bus traffic allowed. Comments are stripped
# before matching so explanatory comments cannot trip the check.
set -eu

F="shell-ext/faceid@nim/dbusClient.js"
fail=0

# Strip // line comments and /* */ block-comment lines. (Block comments
# in this file occupy whole lines; trailing code after */ on one line
# is not a pattern this file uses.)
CODE="$(sed -e 's://.*$::' -e '/\/\*/,/\*\//d' "$F")"

say() { printf '%s\n' "$CODE" | grep -n "$1" || true; }

if [ -n "$(say '\.call(')" ]; then
    printf '%s\n' "$(say '\.call(')"
    echo "FAIL: $F invokes a D-Bus method (lock screen must be listen-only)"
    fail=1
fi
if [ -n "$(say '<method')" ]; then
    printf '%s\n' "$(say '<method')"
    echo "FAIL: $F declares a D-Bus method in its introspection XML"
    fail=1
fi
for m in TestScan PreviewAnimation Diagnostics 'Retry(' '.retry('; do
    if [ -n "$(say "$m")" ]; then
        printf '%s\n' "$(say "$m")"
        echo "FAIL: $F references daemon call $m"
        fail=1
    fi
done

if [ "$fail" -ne 0 ]; then
    exit 1
fi
echo "listen-only OK: no method calls from the lock-screen extension"
