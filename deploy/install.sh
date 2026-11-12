#!/bin/bash
# Install or update optout on this machine: Debian/Ubuntu with systemd, run as root.
# The checkout has to live at /opt/optout (the systemd units and wrappers point there).
# Safe to re-run: existing files in /etc/optout are never overwritten.
set -eu
cd "$(dirname "$(readlink -f "$0")")/.."
[ "$(id -u)" = 0 ] || { echo "install.sh: run as root" >&2; exit 1; }
[ "$(pwd -P)" = "$(readlink -f /opt/optout)" ] || { echo "install.sh: this checkout must be at /opt/optout (it is at $(pwd -P))" >&2; exit 1; }

command -v uv >/dev/null || { apt-get -q update && DEBIAN_FRONTEND=noninteractive apt-get -qy install curl ca-certificates sqlite3 >/dev/null && curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh >/dev/null; }
[ -d .venv ] || uv venv -q -p 3.13 .venv
uv pip install -q -p .venv -e ".[forms]"
# playwright's bundled node needs libatomic, which minimal Debian images do not have
dpkg -s libatomic1 >/dev/null 2>&1 || { apt-get -q update && DEBIAN_FRONTEND=noninteractive apt-get -qy install libatomic1 >/dev/null; }
.venv/bin/python -m playwright install --with-deps chromium >/var/log/optout-playwright.log 2>&1 || echo "playwright browser install failed (see /var/log/optout-playwright.log): web forms will not run until it works"
install -m 755 deploy/optout-run deploy/optout-secrets /usr/local/bin/
mkdir -p /usr/lib/check_mk_agent/local && install -m 755 deploy/optout-check /usr/lib/check_mk_agent/local/optout
install -d -m 700 /etc/optout /etc/optout/auth /var/lib/optout
first=
[ -f /etc/optout/optout.env ] || { install -m 600 deploy/optout.env.example /etc/optout/optout.env; first=1; }
[ -f /etc/optout/people.yaml ] || install -m 600 deploy/people.yaml.example /etc/optout/people.yaml
install -m 644 deploy/*.service deploy/*.timer /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q --now optout-cycle.timer optout-inbox.timer optout-forms.timer
systemctl enable -q optout-web.service && systemctl restart optout-web.service
echo deployed
[ -z "$first" ] || echo "first install: edit /etc/optout/people.yaml, then run optout-secrets (see README.md)"
