#!/bin/sh
# TallyHQ entrypoint.
#
# On boot:
#   1. If /data/conductor.duckdb already exists, just start the web app.
#   2. If it's missing AND $SEED_DB_URL is set, fetch the DB to that path,
#      then start the web app. Use this for one-time bootstrapping a new
#      Railway volume from a public URL (GitHub Release asset, etc.).
#   3. Otherwise start with an empty DB — the app boots fine; the cron
#      service (or manual `politics ...` runs) populates it.
#
# Idempotent: never overwrites an existing DB.
set -e

DB_PATH="${CONDUCTOR_DB:-/data/conductor.duckdb}"
DB_DIR="$(dirname "$DB_PATH")"
mkdir -p "$DB_DIR"

# Marker file indicates a force-seed has already been applied on this volume.
# Once written, future boots skip the force-seed branch even if SEED_DB_FORCE=1
# stays set — so leaving the env var on doesn't re-download every restart.
FORCE_MARKER="$DB_DIR/.seed-force-applied"

# Empty schema-only DuckDB ~12 KB; real seeded one is many MB.
MIN_VALID_BYTES=1048576

EXISTING_SIZE=0
if [ -f "$DB_PATH" ]; then
    EXISTING_SIZE=$(stat -c%s "$DB_PATH" 2>/dev/null || echo 0)
fi

# Resolve which branch to take.
SHOULD_FORCE_SEED=0
if [ "$SEED_DB_FORCE" = "1" ] && [ -n "$SEED_DB_URL" ] && [ ! -f "$FORCE_MARKER" ]; then
    SHOULD_FORCE_SEED=1
fi

if [ "$SHOULD_FORCE_SEED" = "1" ]; then
    echo "[entrypoint] SEED_DB_FORCE=1 set + no marker; replacing existing DB ($EXISTING_SIZE bytes)"
    rm -f "$DB_PATH" "$DB_PATH.wal"
    echo "[entrypoint] fetching seed DB from \$SEED_DB_URL -> $DB_PATH"
    if command -v curl > /dev/null; then
        curl -fL --retry 3 --retry-delay 2 -o "$DB_PATH.partial" "$SEED_DB_URL"
    else
        wget --tries=3 -O "$DB_PATH.partial" "$SEED_DB_URL"
    fi
    mv "$DB_PATH.partial" "$DB_PATH"
    SIZE=$(stat -c%s "$DB_PATH" 2>/dev/null || echo 0)
    echo "[entrypoint] force-seeded DB ($SIZE bytes)"
    # Write marker so we never re-force-seed this volume even if env var stays set.
    date -u +"%Y-%m-%dT%H:%M:%SZ" > "$FORCE_MARKER"
    echo "[entrypoint] marker written: $FORCE_MARKER"
elif [ "$EXISTING_SIZE" -ge "$MIN_VALID_BYTES" ]; then
    echo "[entrypoint] using existing DB at $DB_PATH ($EXISTING_SIZE bytes)"
    if [ "$SEED_DB_FORCE" = "1" ] && [ -f "$FORCE_MARKER" ]; then
        echo "[entrypoint] SEED_DB_FORCE=1 ignored — already applied (marker present)"
    fi
elif [ -n "$SEED_DB_URL" ]; then
    if [ "$EXISTING_SIZE" -gt 0 ]; then
        echo "[entrypoint] existing DB is too small ($EXISTING_SIZE bytes); replacing"
        rm -f "$DB_PATH" "$DB_PATH.wal"
    fi
    echo "[entrypoint] fetching seed DB from \$SEED_DB_URL -> $DB_PATH"
    if command -v curl > /dev/null; then
        curl -fL --retry 3 --retry-delay 2 -o "$DB_PATH.partial" "$SEED_DB_URL"
    else
        wget --tries=3 -O "$DB_PATH.partial" "$SEED_DB_URL"
    fi
    mv "$DB_PATH.partial" "$DB_PATH"
    SIZE=$(stat -c%s "$DB_PATH" 2>/dev/null || echo 0)
    echo "[entrypoint] seeded DB ($SIZE bytes)"
else
    echo "[entrypoint] no DB and no SEED_DB_URL — starting with empty DB"
fi

# Hand off to whatever the deploy command is (defaults to web)
exec "$@"
