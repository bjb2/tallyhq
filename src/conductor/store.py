"""DuckDB-backed append-only event store.

Schema is intentionally narrow — `payload` is JSON, queryable via DuckDB's
JSON functions. Curation lives in SQL views, not Python.

Dedupe: an Event is only inserted if no prior row exists with matching
(source, source_id, payload_hash). This means re-running an adapter is safe
(idempotent) and only writes when the source's payload actually changed.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

import duckdb

from conductor.events import Event

DEFAULT_DB_PATH = Path("data/conductor.duckdb")

SCHEMA_SQL = """
CREATE SEQUENCE IF NOT EXISTS events_id_seq START 1;

CREATE TABLE IF NOT EXISTS events (
    id              BIGINT PRIMARY KEY DEFAULT nextval('events_id_seq'),
    source          VARCHAR NOT NULL,
    source_id       VARCHAR NOT NULL,
    entity_id       VARCHAR NOT NULL,
    event_type      VARCHAR NOT NULL,
    observed_at     TIMESTAMPTZ NOT NULL,
    occurred_at     TIMESTAMPTZ NOT NULL,
    payload_hash    VARCHAR NOT NULL,
    payload         JSON NOT NULL,
    schema_version  INTEGER NOT NULL DEFAULT 1
);

CREATE INDEX IF NOT EXISTS idx_events_source_sid ON events (source, source_id);
CREATE INDEX IF NOT EXISTS idx_events_entity ON events (entity_id);
CREATE INDEX IF NOT EXISTS idx_events_type ON events (event_type);
CREATE INDEX IF NOT EXISTS idx_events_occurred ON events (occurred_at);

CREATE TABLE IF NOT EXISTS adapter_cursor (
    source          VARCHAR PRIMARY KEY,
    cursor          VARCHAR,
    updated_at      TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS cache (
    cache_key       VARCHAR PRIMARY KEY,
    payload         JSON NOT NULL,
    fetched_at      TIMESTAMPTZ NOT NULL,
    expires_at      TIMESTAMPTZ
);
"""


class Store:
    def __init__(self, db_path: Path = DEFAULT_DB_PATH, *, read_only: bool = False):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.read_only = read_only
        self.conn = duckdb.connect(str(db_path), read_only=read_only)
        if not read_only:
            # Schema bootstrap requires R/W. Read-only connections assume the
            # file already has the schema (true in normal web operation).
            self.conn.execute(SCHEMA_SQL)

    def close(self):
        self.conn.close()

    def insert_events(self, events: Iterable[Event]) -> int:
        """Insert events, skipping any whose (source, source_id, payload_hash)
        already exists. Returns count of newly-inserted rows.
        """
        inserted = 0
        for ev in events:
            existing = self.conn.execute(
                "SELECT 1 FROM events WHERE source = ? AND source_id = ? AND payload_hash = ? LIMIT 1",
                [ev.source, ev.source_id, ev.payload_hash],
            ).fetchone()
            if existing:
                continue
            self.conn.execute(
                """
                INSERT INTO events
                    (source, source_id, entity_id, event_type, observed_at, occurred_at,
                     payload_hash, payload, schema_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    ev.source,
                    ev.source_id,
                    ev.entity_id,
                    ev.event_type,
                    ev.observed_at,
                    ev.occurred_at,
                    ev.payload_hash,
                    json.dumps(ev.payload, default=str),
                    ev.schema_version,
                ],
            )
            inserted += 1
        return inserted

    def get_cursor(self, source: str) -> str | None:
        row = self.conn.execute(
            "SELECT cursor FROM adapter_cursor WHERE source = ?", [source]
        ).fetchone()
        return row[0] if row else None

    def set_cursor(self, source: str, cursor: str) -> None:
        self.conn.execute(
            """
            INSERT INTO adapter_cursor (source, cursor, updated_at)
            VALUES (?, ?, NOW())
            ON CONFLICT (source) DO UPDATE
                SET cursor = excluded.cursor, updated_at = excluded.updated_at
            """,
            [source, cursor],
        )

    def cache_get(self, key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT payload, expires_at FROM cache WHERE cache_key = ?", [key]
        ).fetchone()
        if not row:
            return None
        payload, expires_at = row
        if expires_at is not None:
            from datetime import datetime, timezone
            if datetime.now(tz=timezone.utc) > expires_at:
                return None
        return json.loads(payload)

    def cache_put(self, key: str, payload: dict, ttl_seconds: int | None = None) -> None:
        from datetime import datetime, timedelta, timezone
        now = datetime.now(tz=timezone.utc)
        expires_at = now + timedelta(seconds=ttl_seconds) if ttl_seconds else None
        self.conn.execute(
            """
            INSERT INTO cache (cache_key, payload, fetched_at, expires_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (cache_key) DO UPDATE
                SET payload = excluded.payload,
                    fetched_at = excluded.fetched_at,
                    expires_at = excluded.expires_at
            """,
            [key, json.dumps(payload, default=str), now, expires_at],
        )

    def latest_payload(self, source: str, source_id: str) -> dict | None:
        """Return the latest payload for a (source, source_id) pair, if any."""
        row = self.conn.execute(
            """
            SELECT payload FROM events
            WHERE source = ? AND source_id = ?
            ORDER BY observed_at DESC LIMIT 1
            """,
            [source, source_id],
        ).fetchone()
        return json.loads(row[0]) if row else None

    def distinct_entities(self, *, event_type: str | None = None) -> list[str]:
        if event_type:
            rows = self.conn.execute(
                "SELECT DISTINCT entity_id FROM events WHERE event_type = ?",
                [event_type],
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT DISTINCT entity_id FROM events").fetchall()
        return [r[0] for r in rows]
