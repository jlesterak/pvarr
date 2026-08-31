#!/usr/bin/env bash
#
# PVArr container entrypoint.
#
# Runs as root only long enough to make the mounted volumes writable, then
# drops to PUID:PGID and execs the app. Nothing runs as root once PVArr starts.
#
# Why this exists: /config, /recordings and /app/logs are bind mounts. A bind
# mount grafts the HOST directory's inode over the image's, so permission
# checks run against the host's ownership and the chown baked into the image
# has no effect. On a fresh clone those directories do not exist at all, so
# dockerd creates them as root:root -- and the app, running unprivileged, then
# cannot write a single recording.
set -euo pipefail

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"
APP_USER="pvarr"
VOLUMES=(/config /recordings /app/logs)

log() { echo "[entrypoint] $*"; }

# Already unprivileged: someone set `user:` in compose, or this is a rootless
# runtime. We cannot fix ownership from here, so verify and fail loudly rather
# than starting and dying mid-recording.
if [ "$(id -u)" -ne 0 ]; then
    log "Running as uid $(id -u); skipping ownership setup."
    failed=0
    for d in "${VOLUMES[@]}"; do
        [ -d "$d" ] || continue
        if [ ! -w "$d" ]; then
            log "ERROR: $d is not writable by uid $(id -u)."
            log "       That is a bind mount; fix the HOST directory it maps to"
            log "       in docker-compose.yml, e.g.:"
            log "         sudo chown -R $(id -u):$(id -g) ./config ./recordings ./logs"
            failed=1
        fi
    done
    [ "$failed" -eq 0 ] || exit 1
    exec "$@"
fi

# Align the app user with the host's uid/gid so files land owned by the right
# person on the other side of the mount.
current_uid="$(id -u "$APP_USER")"
current_gid="$(id -g "$APP_USER")"
if [ "$current_gid" != "$PGID" ]; then
    groupmod -o -g "$PGID" "$APP_USER"
fi
if [ "$current_uid" != "$PUID" ]; then
    usermod -o -u "$PUID" "$APP_USER"
fi
[ "$current_uid" = "$PUID" ] && [ "$current_gid" = "$PGID" ] || \
    log "Running as ${APP_USER} (${PUID}:${PGID})."

# Non-recursive on purpose. A recursive chown of /recordings would be a
# multi-terabyte metadata walk on every boot for a media library, and the
# contents are already owned correctly once the mount root is.
for d in "${VOLUMES[@]}"; do
    mkdir -p "$d"
    if ! gosu "${PUID}:${PGID}" test -w "$d"; then
        log "Fixing ownership of $d (was $(stat -c '%u:%g' "$d"))."
        chown "${PUID}:${PGID}" "$d"
    fi
done

# /app itself must stay writable for the venv-skip path and any scratch files.
chown "${PUID}:${PGID}" /app 2>/dev/null || true

exec gosu "${PUID}:${PGID}" "$@"
