#!/bin/sh
# Start as root only long enough to make the bind-mounted data dir writable
# (Docker creates missing host dirs root-owned, and pre-existing deployments
# have root-owned files from when the container ran as root), then drop to
# the unprivileged app user. Uses python to drop privileges since gosu/setpriv
# aren't guaranteed in the slim image.
set -e
if [ "$(id -u)" = "0" ]; then
    # Best-effort: a read-only or root-squashed data mount shouldn't crash-loop
    # the container. If chown fails, writes may fail later with a clear error.
    chown -R app:app /srv/data 2>/dev/null \
        || echo "entrypoint: warning: could not chown /srv/data (continuing)" >&2
    # Derive uid/gid from the app user rather than hardcoding 1000 — useradd
    # only guarantees the uid, not that the auto-created group's gid is 1000.
    exec python3 -c 'import os, pwd, sys
p = pwd.getpwnam("app")
os.setgroups([p.pw_gid])
os.setgid(p.pw_gid)
os.setuid(p.pw_uid)
os.execvp(sys.argv[1], sys.argv[1:])' "$@"
fi
exec "$@"
