"""Federal legislator entity model + DB schema.

Entity table is separate from the events table — events are append-only
observations, entities are mutable state (current term, party, committees).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from conductor.store import Store

LEGISLATORS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS legislators (
    bioguide_id     VARCHAR PRIMARY KEY,
    first_name      VARCHAR,
    last_name       VARCHAR,
    full_name       VARCHAR,
    chamber         VARCHAR,        -- 'house' | 'senate'
    state           VARCHAR,
    district        INTEGER,        -- NULL for senate
    party           VARCHAR,
    served_from     DATE,
    served_until    DATE,
    ids             JSON,           -- crosswalk: govtrack, fec, opensecrets, icpsr, ...
    updated_at      TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_legislators_chamber ON legislators (chamber);
CREATE INDEX IF NOT EXISTS idx_legislators_state ON legislators (state);
"""


@dataclass
class FederalEntity:
    bioguide_id: str
    first_name: str
    last_name: str
    full_name: str
    chamber: Literal["house", "senate"]
    state: str
    district: int | None
    party: str
    served_from: date
    served_until: date | None
    ids: dict

    @property
    def entity_id(self) -> str:
        return f"bioguide:{self.bioguide_id}"


def ensure_schema(store: Store) -> None:
    if getattr(store, "read_only", False):
        return
    store.conn.execute(LEGISLATORS_SCHEMA_SQL)


def upsert_legislators(store: Store, entities: list[FederalEntity]) -> int:
    ensure_schema(store)
    import json
    n = 0
    for e in entities:
        store.conn.execute(
            """
            INSERT INTO legislators
                (bioguide_id, first_name, last_name, full_name, chamber, state,
                 district, party, served_from, served_until, ids, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NOW())
            ON CONFLICT (bioguide_id) DO UPDATE SET
                first_name = excluded.first_name,
                last_name = excluded.last_name,
                full_name = excluded.full_name,
                chamber = excluded.chamber,
                state = excluded.state,
                district = excluded.district,
                party = excluded.party,
                served_from = excluded.served_from,
                served_until = excluded.served_until,
                ids = excluded.ids,
                updated_at = NOW()
            """,
            [
                e.bioguide_id,
                e.first_name,
                e.last_name,
                e.full_name,
                e.chamber,
                e.state,
                e.district,
                e.party,
                e.served_from,
                e.served_until,
                json.dumps(e.ids),
            ],
        )
        n += 1
    return n


def get(store: Store, bioguide_id: str) -> FederalEntity | None:
    ensure_schema(store)
    import json
    row = store.conn.execute(
        """
        SELECT bioguide_id, first_name, last_name, full_name, chamber, state,
               district, party, served_from, served_until, ids
        FROM legislators WHERE bioguide_id = ?
        """,
        [bioguide_id],
    ).fetchone()
    if not row:
        return None
    return FederalEntity(
        bioguide_id=row[0], first_name=row[1], last_name=row[2], full_name=row[3],
        chamber=row[4], state=row[5], district=row[6], party=row[7],
        served_from=row[8], served_until=row[9],
        ids=json.loads(row[10]) if row[10] else {},
    )


def list_all(
    store: Store,
    chamber: str | None = None,
    state: str | None = None,
    active_only: bool = True,
) -> list[FederalEntity]:
    ensure_schema(store)
    import json
    where = []
    params: list = []
    if chamber:
        where.append("chamber = ?")
        params.append(chamber)
    if state:
        where.append("state = ?")
        params.append(state)
    if active_only:
        where.append("(served_until IS NULL OR served_until >= CURRENT_DATE)")
    sql = """
        SELECT bioguide_id, first_name, last_name, full_name, chamber, state,
               district, party, served_from, served_until, ids
        FROM legislators
    """
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY chamber, state, last_name"
    rows = store.conn.execute(sql, params).fetchall()
    return [
        FederalEntity(
            bioguide_id=r[0], first_name=r[1], last_name=r[2], full_name=r[3],
            chamber=r[4], state=r[5], district=r[6], party=r[7],
            served_from=r[8], served_until=r[9],
            ids=json.loads(r[10]) if r[10] else {},
        )
        for r in rows
    ]
