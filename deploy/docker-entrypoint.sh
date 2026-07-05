#!/bin/sh
# Start as root only long enough to make the bind-mounted data dir writable
# (Docker creates missing host dirs root-owned, and pre-existing deployments
# have root-owned files from when the container ran as root), then drop to
# the unprivileged app user. Uses python to drop privileges since gosu/setpriv
# aren't guaranteed in the slim image.
set -e
if [ "$(id -u)" = "0" ]; then
    chown -R app:app /srv/data
    exec python3 -c 'import os, sys
os.setgroups([1000])
os.setgid(1000)
os.setuid(1000)
os.execvp(sys.argv[1], sys.argv[1:])' "$@"
fi
exec "$@"
