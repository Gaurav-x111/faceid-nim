# UI rewrite — where each file goes

All paths relative to the repo root. Overwrite the existing files.

## Lock-screen scan UI (the Glance-style pill)

```
shell-ext/faceid@nim/faceGlyph.js     NEW    Cairo Face ID glyph: rays → checkmark → padlock
shell-ext/faceid@nim/pill.js          REPLACE  Dynamic-Island container + animation clock
shell-ext/faceid@nim/stylesheet.css   REPLACE  pill surface, ok/fail tints, typography
shell-ext/faceid@nim/extension.js     REPLACE  state mapping, hover-to-retry wiring
shell-ext/faceid@nim/dbusClient.js    PATCHED  + retry() and TestScan in the introspection XML
```

Nothing else in `shell-ext/` changes. `metadata.json` is untouched —
`session-modes` already includes `unlock-dialog`, which is what keeps
the extension alive on the lock screen.

### The animation, in order

| State | What the glyph does | Duration |
|---|---|---|
| `waking` / `searching` | pill expands from a 52px capsule to 268px; rays sweep | 340ms expand, then loops |
| `verifying` | rays keep sweeping, progress feeds brightness | continuous |
| `matched` | rays collapse inward, checkmark draws itself in two strokes | 620ms |
| → auto | checkmark shrinks, padlock scales in and its shackle swings open | 720ms |
| `rejected` / `timeout` / `camera_error` | rays freeze red, pill shakes, "Hover to try again" | 1.5s then contract |

One `GLib.timeout_add` at 16ms drives everything. The glyph owns no
timers — it is handed a phase and a normalized `t` — so the checkmark
can never finish before the container has stopped growing.

## App UI

```
app/faceid_app/main.py      REPLACE  sidebar nav, hero card, four pages
app/faceid_app/style.css    NEW      hero card + status dots + preview frame only
app/faceid_app/onboarding.py PATCHED rounded COVER preview, big pose name, per-pose dots
packaging/debian/rules      PATCHED  installs style.css alongside main.py
```

The app deliberately does **not** copy Glance's look. Glance is a macOS
menu-bar app; a Linux settings app that imitates macOS chrome looks
wrong next to GNOME Settings. So the app follows Adwaita conventions
(sidebar + boxed lists + `Adw.Clamp`) and only adds custom CSS for the
three things Adwaita has no widget for: the hero card, the status dots,
and the rounded camera preview.

Pages: **Overview** (hero on/off card + enrolled faces + enroll/test),
**Settings** (strictness with a live explanation banner, camera source,
timeout, attention), **Security** (the honest limitations, plus delete
all data), **Diagnostics** (daemon state + copyable CLI commands).

## Two things to check after you drop these in

1. `_promptBox()` in `extension.js` reaches into `Main.screenShield._dialog._authPrompt._mainBox`.
   That is a private path and the one part of the extension that can
   break on a GNOME upgrade. Verify it on your GNOME version in a
   nested shell before trusting the demo.
2. The pill is inserted at index 0 of the prompt box, above the
   password entry. If your unlock dialog puts the user avatar there,
   bump the index.

## Testing the animation without locking yourself out

```sh
gnome-extensions pack --force --out-dir=/tmp shell-ext/faceid@nim
gnome-extensions install --force /tmp/faceid@nim.shell-extension.zip
dbus-run-session -- gnome-shell --nested --wayland
# in that session:
loginctl lock-session
journalctl -f -o cat /usr/bin/gnome-shell
```

To drive the states without a working daemon, emit the signal by hand:

```sh
sudo dbus-send --system --type=signal /org/faceidnim/Daemon1 \
  org.faceidnim.Daemon1.ScanState string:"searching" double:0.0 string:""
sudo dbus-send --system --type=signal /org/faceidnim/Daemon1 \
  org.faceidnim.Daemon1.ScanState string:"matched" double:1.0 string:""
```

That is also how you record the demo GIF before the Rust daemon
compiles — the animation is fully exercisable on its own.
