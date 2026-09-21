# Convenience targets. The .deb build is what release.yml calls.
PREFIX     ?= /usr
LIBEXEC    := $(PREFIX)/libexec/faceid-nim
SECURITYDIR?= /lib/x86_64-linux-gnu/security

.PHONY: all daemon pam vision test clean install deb dev-extension

all: daemon pam

daemon:
	cd daemon && cargo build --release

pam:
	gcc -shared -fPIC -std=c11 -Wall -Wextra -Werror \
	    -o pam/pam_faceid.so pam/pam_faceid.c -lpam

test:
	cd daemon && cargo test
	cd vision && python3 -m pytest -q tests

install: all
	install -d $(DESTDIR)$(LIBEXEC) $(DESTDIR)$(SECURITYDIR)
	install -m 0755 daemon/target/release/faceid-nimd $(DESTDIR)$(LIBEXEC)/
	install -m 0644 pam/pam_faceid.so $(DESTDIR)$(SECURITYDIR)/
	install -d $(DESTDIR)$(PREFIX)/share/faceid-nim/models
	install -m 0644 models/manifest.toml $(DESTDIR)$(PREFIX)/share/faceid-nim/models/
	install -d $(DESTDIR)$(PREFIX)/share/gnome-shell/extensions/faceid@nim
	cp -r shell-ext/faceid@nim/* $(DESTDIR)$(PREFIX)/share/gnome-shell/extensions/faceid@nim/
	install -d $(DESTDIR)/lib/systemd/system
	install -m 0644 packaging/systemd/*.service $(DESTDIR)/lib/systemd/system/
	install -d $(DESTDIR)/usr/share/dbus-1/system.d
	install -m 0644 packaging/dbus/*.conf $(DESTDIR)/usr/share/dbus-1/system.d/
	install -d $(DESTDIR)/usr/share/polkit-1/actions
	install -m 0644 packaging/polkit/*.policy $(DESTDIR)/usr/share/polkit-1/actions/
	install -d $(DESTDIR)/usr/share/pam-configs
	install -m 0644 packaging/pam-configs/faceid-nim $(DESTDIR)/usr/share/pam-configs/
	# Bundled venv so users never run pip.
	python3 -m venv $(DESTDIR)$(LIBEXEC)/venv
	$(DESTDIR)$(LIBEXEC)/venv/bin/pip install --no-cache-dir ./vision
	# Settings GUI + launcher. The wrapper runs the app on the system
	# python (the venv above is for the worker only and has no GTK).
	install -d $(DESTDIR)$(PREFIX)/share/faceid-nim/app/faceid_app
	cp -r app/faceid_app/. $(DESTDIR)$(PREFIX)/share/faceid-nim/app/faceid_app/
	install -m 0755 packaging/faceid-app $(DESTDIR)/usr/bin/faceid-app
	install -d $(DESTDIR)$(PREFIX)/share/applications
	install -m 0644 app/data/org.faceidnim.App.desktop $(DESTDIR)$(PREFIX)/share/applications/

deb:
	dpkg-buildpackage -us -uc -b
	mkdir -p dist && mv ../faceid-nim_*.deb dist/ 2>/dev/null || true

# Install the extension for the current user and watch the shell log.
dev-extension:
	gnome-extensions pack --force --out-dir=/tmp \
	    --extra-source=pill.js \
	    --extra-source=opening.js \
	    --extra-source=faceGlyph.js \
	    --extra-source=dbusClient.js \
	    --extra-source=logo.svg \
	    shell-ext/faceid@nim
	gnome-extensions install --force /tmp/faceid@nim.shell-extension.zip
	@echo "Now: dbus-run-session -- gnome-shell --nested --wayland"
	@echo "Then in that session: loginctl lock-session"
	@echo "Logs: journalctl -f -o cat /usr/bin/gnome-shell"

clean:
	cd daemon && cargo clean
	rm -f pam/pam_faceid.so
	rm -rf dist
