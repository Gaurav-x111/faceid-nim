# Live-proof checklist (Phase 2, Step 6)

These six checks need a human at the keyboard of a real machine with
`sudo`: an interactive polkit agent, service restarts, camera
experiments, and a throwaway VM. The auditor (no sudo password, no
spare machine) verified everything short of these in code and unit
tests. **Leave every item BLOCKED until its output is pasted back.**

Run the helper first -- it collects the machine-readable half:

```sh
sudo scripts/live-proofs.sh 2>&1 | tee live-proof-results.log
```

then work through items 1-6 below, appending terminal output to the
same log. Each item states its exact pass criterion.

## 1. Polkit: real password prompt on FinishEnrollment (gate 1A-1)

1. Open the Face Unlock app, start an enrollment, capture at least one
   pose.
2. Click Finish/Save. **Pass criterion:** a system password prompt
   appears (org.faceidnim.enroll, auth_self).
3. Cancel the prompt. **Pass criterion:** the save aborts, nothing is
   written to `/var/lib/faceid-nim/users/<uid>/`, and
   `journalctl -u faceid-nimd --since "5 minutes ago"` shows a polkit
   denial (`not authorized`) rather than a traceback.
4. Repeat and this time authenticate. **Pass criterion:** templates
   persist and the app reports success.

## 2. Socket continuity across `restart faceid-nimd` (gate 1A-3)

```sh
sudo systemctl restart faceid-nimd
sleep 3
faceid-nim status
```

**Pass criteria:** `faceid-nimd: active`, `faceid-vision: active`,
both sockets present, and a Test scan from the app returns
`recognised`/`not recognised` -- with **no** manual
`systemctl restart faceid-vision`. Paste the full `status` output.

## 3. PAM fallthrough drill (recovery, 1C)

Keep a root shell open in a second terminal before you start (if the
stack misbehaves you can revert with `pam-auth-update --package
--remove faceid-nim`).

```sh
sudo systemctl stop faceid-vision
sudo -k && time sudo true
sudo systemctl start faceid-vision
sudo systemctl stop faceid-nimd
sudo -k && time sudo true
sudo systemctl start faceid-nimd faceid-vision
```

**Pass criteria:** each `sudo true` shows the **password prompt
within ~8 seconds** and succeeds with the correct password; the face
attempt degrades invisibly (no hang, no lockout). Paste the `time`
output for both stops.

## 4. Lock-screen pill runtime test (Cairo exercise)

`node --check` (CI) only parses the extension -- it never runs a
Cairo repaint. On the machine:

1. Pack and install the extension, enable it, lock the screen
   (nested `gnome-shell` in a Wayland session is fine).
2. From an SSH session or VT, drive every ScanState while tailing the
   shell log:

```sh
journalctl -f -o cat /usr/bin/gnome-shell &
for s in searching matched rejected timeout camera_error; do
  dbus-send --system --type=signal /org/faceidnim/Daemon1 \
    org.faceidnim.Daemon1.ScanState string:"$s" double:0.5 string:"proof"
  sleep 3
done
```

**Pass criteria:** the pill animates through scanning → each terminal
state with no exception. **Any Cairo traceback must be pasted verbatim
with its line number** -- that is a finding, not a pass.

## 5. Purge test (lifecycle, 1D) -- THROWAWAY VM OR CONTAINER ONLY

Do NOT run this on a daily-driver machine: it deletes enrollments.

1. In a throwaway Ubuntu 24.04 VM/container: install the `.deb`,
   enroll one identity, confirm `faceid-nim status` is healthy.
2. `sudo apt purge faceid-nim`, then verify each of these is gone:

```sh
ls /var/lib/faceid-nim /etc/faceid-nim               # must not exist
grep -r faceid /etc/pam.d/                            # no references
systemctl cat faceid-nimd.service faceid-vision.service  # not-found
ls /usr/share/dbus-1/system.d/org.faceidnim.Daemon1.conf \
   /usr/share/polkit-1/actions/org.faceidnim.policy  # not-found
getent passwd faceid                                  # no user
```

**Pass criterion:** all five checks show absence. Paste the output.

## 6. Small soak (resources, 1F)

`scripts/live-proofs.sh soak` runs 200 `sudo -k && sudo true`
iterations (password from the interactive agent -- stay nearby),
sampling daemon RSS, fd count (`ls /proc/<pid>/fd | wc -l`), audit log
size, and per-attempt latency at start, middle, and end.

**Pass criteria:** zero hangs (every attempt resolves in < 12 s),
RSS growth < 10% end-vs-start, fd count stable (±5), audit log bounded
(≤ 50 MB cap, no journal spam). Paste the start/end table.
