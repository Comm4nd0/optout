#!/bin/bash
# deploy.sh [host] [ctid]: push this checkout into a Proxmox LXC container and (re)install it there.
# host is the Proxmox node as ssh sees it, ctid the container. Defaults come from .deploy.env
# beside this script (gitignored), e.g.   HOST=root@pve.example   CT=101
# Not on Proxmox? Put the checkout at /opt/optout on the target machine and run deploy/install.sh.
set -eu
D=$(dirname "$(readlink -f "$0")")
[ -f "$D/.deploy.env" ] && . "$D/.deploy.env"
HOST=${1:-${HOST:-}} CT=${2:-${CT:-}}
[ -n "$HOST" ] && [ -n "$CT" ] || { echo "usage: deploy.sh <ssh-host> <ctid>   (or set HOST and CT in .deploy.env)" >&2; exit 2; }
run() { ssh -o BatchMode=yes "$HOST" "pct exec $CT -- bash -lc $(printf %q "$1")"; }

tar -C "$D" --exclude=.venv --exclude=.git --exclude=__pycache__ --exclude=.pytest_cache --exclude=.deploy.env \
    --exclude=dist --exclude='tools/*.csv' -czf - . |
  ssh -o BatchMode=yes "$HOST" "pct exec $CT -- bash -c 'rm -rf /opt/optout.new && mkdir -p /opt/optout.new && tar -xzf - -C /opt/optout.new'"

run '
set -e
[ -d /opt/optout/.venv ] && mv /opt/optout/.venv /opt/optout.new/.venv
rm -rf /opt/optout && mv /opt/optout.new /opt/optout
exec bash /opt/optout/deploy/install.sh
'
