# Where each new file goes

Unpack over the existing repo root; every path is relative to it.

## New files

```
bin/faceid-nim                                   (new dir)  POSIX-sh CLI → /usr/bin/faceid-nim
packaging/debian/rules                                      dpkg-buildpackage entry point
packaging/debian/changelog
packaging/debian/compat
packaging/debian/source/format                   (new dir)
packaging/scripts/fetch-models                              → /usr/libexec/faceid-nim/fetch-models
packaging/scripts/verify-models                             → /usr/libexec/faceid-nim/verify-models
packaging/logrotate/faceid-nim                   (new dir)  → /etc/logrotate.d/faceid-nim
app/data/org.faceidnim.App.desktop                          → /usr/share/applications/
app/data/org.faceidnim.App.metainfo.xml                     → /usr/share/metainfo/
app/data/gschema.xml                                        → /usr/share/glib-2.0/schemas/org.faceidnim.settings.gschema.xml
app/data/icons/hicolor/scalable/apps/org.faceidnim.App.svg
app/data/icons/hicolor/symbolic/status/faceid-scanning-symbolic.svg
app/data/icons/hicolor/symbolic/status/faceid-unlocked-symbolic.svg
daemon/src/enroll.rs                                        register it yourself in main.rs
vision/faceid_vision/enrollpreview.py
app/faceid_app/onboarding.py
docs/enrollment-interface.md
docs/PATHS.md
```

## Overwritten

```
packaging/debian/postinst                  rewritten (user, dirs, models, PAM, services)
packaging/systemd/faceid-nimd.service      + worker runtime subdir, + /etc write
packaging/systemd/faceid-vision.service    socket moved to /run/faceid-nim/worker/
```

## Patched in place (small, marked edits)

```
vision/faceid_vision/scan.py       ScanConfig.preview; one ENROLL PREVIEW INTEGRATION HOOK block
vision/faceid_vision/__main__.py   one line: preview= in the ScanConfig constructor
app/faceid_app/dbus_client.py      appended: class Enrollment
app/faceid_app/main.py             on_enroll now opens OnboardingWindow (old _do_enroll removed)
```

## Untouched, as instructed

`daemon/src/dbus.rs`, `daemon/src/session.rs`, `daemon/src/main.rs`,
`daemon/src/ipc.rs`, `models/manifest.toml`, `.github/*`, `shell-ext/*`,
`packaging/debian/{control,prerm,postrm}`, `packaging/{dbus,polkit,pam-configs}`,
`packaging/scripts/install.sh`, `vision/tests/*`.

## Two things you have to do yourself

1. **Register the enrollment object.** In `main.rs`, next to where
   `Daemon1` is served:

   ```rust
   mod enroll;
   conn.object_server()
       .at("/org/faceidnim/Enrollment1",
           enroll::Enrollment1::new(cfg.clone(), store.clone(),
                                    worker.clone(), conn.clone()))
       .await?;
   ```

2. **Allow the new interface on the system bus.** `packaging/dbus/org.faceidnim.Daemon1.conf`
   only allows `send_interface="org.faceidnim.Daemon1"`. Add the same
   `send_destination` allows for `org.faceidnim.Enrollment1`, plus a
   `receive_sender` allow for its signal, or every call from the app is
   refused before it reaches the daemon.

Preview frames also need the daemon's worker-event reader to call
`Enrollment1::deliver_preview(session, uid, jpeg)` when a worker line
carries `{"ev":"preview","jpeg":...}`. That reader lives in
`worker.rs`/`session.rs`, which you asked me not to touch — the
receiving end is written and waiting.
