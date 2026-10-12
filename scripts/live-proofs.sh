#!/bin/sh
# live-proofs.sh -- machine-readable half of docs/live-proof-checklist.md
#
# Run interactively with sudo; it WRITES nothing except the log file
# you redirect it to. Read-only probes, plus the metric sampling for
# checklist item 6 ("soak").
#
#   sudo scripts/live-proofs.sh 2>&1 | tee live-proof-results.log
#   sudo scripts/live-proofs.sh soak 2>&1 | tee -a live-proof-results.log
set -eu

say() { printf '\n### %s\n' "$*"; }

say "versions"
dpkg -l faceid-nim 2>/dev/null | tail -1 || echo "not installed via dpkg"
faceid-nimd --version 2>/dev/null || echo "(daemon has no --version; skipping)"

say "unit states"
systemctl is-active faceid-nimd.service faceid-vision.service \
    faceid-nim-fetch-models.service || true
systemctl show faceid-nimd.service -p RuntimeDirectoryPreserve

say "sockets"
ls -l /run/faceid-nim/auth.sock /run/faceid-nim/worker/vision.sock || true

say "bus name owner"
busctl --system call org.freedesktop.DBus /org/freedesktop/DBus \
    org.freedesktop.DBus NameHasOwner s org.faceidnim.Daemon1 || true

say "polkit actions present"
ls /usr/share/polkit-1/actions/org.faceidnim.policy || true
grep -o 'action id="[^"]*"' /usr/share/polkit-1/actions/org.faceidnim.policy \
    2>/dev/null || true

say "models verify"
faceid-nim verify 2>&1 | tail -8 || true

say "status (unprivileged view)"
faceid-nim status 2>&1 | head -30 || true

say "camera ACLs (residual-risk evidence)"
ls -l /dev/video* 2>/dev/null || echo "no /dev/videoN nodes"
getfacl /dev/video0 2>/dev/null || echo "(getfacl unavailable)"

if [ "${1:-}" = "soak" ]; then
    say "soak: 200 password fallthrough attempts"
    pid="$(systemctl show faceid-nimd.service -p MainPID --value)"
    sample() {
        rss="$(awk '/VmRSS/{print $2 $3}' /proc/"$pid"/status 2>/dev/null || echo ?)"
        fds="$(ls /proc/"$pid"/fd 2>/dev/null | wc -l)"
        logsz="$(stat -c %s /var/log/faceid-nim/audit.log 2>/dev/null || echo ?)"
        printf 'iter=%s rss=%s fds=%s audit_bytes=%s\n' "$1" "$rss" "$fds" "$logsz"
    }
    sample 0
    i=0
    while [ "$i" -lt 200 ]; do
        i=$((i + 1))
        start="$(date +%s)"
        if ! sudo -k true </dev/tty >/dev/tty 2>&1; then
            echo "iter=$i FAILED or timed out after $(($(date +%s) - start))s"
        fi
        if [ "$i" = 100 ]; then sample 100; fi
    done
    sample 200
    echo "soak done: review rss/fds/audit_bytes drift above"
fi
