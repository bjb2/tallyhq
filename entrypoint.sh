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

if [ -f "$DB_PATH" ]; then
    SIZE=$(stat -c%s "$DB_PATH" 2>/dev/null || echo 0)
    echo "[entrypoint] using existing DB at $DB_PATH ($SIZE bytes)"
elif [ -n "$SEED_DB_URL" ]; then
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
