"""Bill entity + helpers.

Bills are first-class entities (like legislators) — mutable state with
introduced date, latest action, sponsor, cosponsor count. Updated on every
adapter pull. Roll-call vote events join to bills via parsed legis_num.

bill_id format: "{congress}:{type}:{number}" e.g. "119:hr:1234".
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from conductor.store import Store

BILLS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bills (
    bill_id              VARCHAR PRIMARY KEY,    -- "119:hr:1234"
    congress             INTEGER,
    bill_type            VARCHAR,                -- 'hr', 's', 'hjres', 'sjres', 'hconres', 'sconres', 'hres', 'sres'
    number               INTEGER,
    title                VARCHAR,
    sponsor_bioguide     VARCHAR,
    introduced_date      DATE,
    latest_action_date   DATE,
    latest_action_text   VARCHAR,
    policy_area          VARCHAR,
    url                  VARCHAR,
    text_versions        JSON,                   -- [{type, format, url, date}]
    cosponsor_count      INTEGER,
    updated_at           TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bills_sponsor ON bills (sponsor_bioguide);
CREATE INDEX IF NOT EXISTS idx_bills_action ON bills (latest_action_date);
CREATE INDEX IF NOT EXISTS idx_bills_congress ON bills (congress);
"""


@dataclass
class Bill:
    bill_id: str
    congress: int
    bill_type: str
    number: int
    title: str
    sponsor_bioguide: Optional[str]
    introduced_date: Optional[date]
    latest_action_date: Optional[date]
    latest_action_text: str
    policy_area: str
    url: str
    text_versions: list[dict] = field(default_factory=list)
    cosponsor_count: int = 0


def ensure_schema(store: Store) -> None:
    store.conn.execute(BILLS_SCHEMA_SQL)


def upsert(store: Store, b: Bill) -> None:
    ensure_schema(store)
    store.conn.execute(
        """
        INSERT INTO bills
            (bill_id, congress, bill_type, number, title, sponsor_bioguide,
             introduced_date, latest_action_date, latest_action_text, policy_area,
             url, text_versions, cosponsor_count, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NOW())
        ON CONFLICT (bill_id) DO UPDATE SET
            congress = excluded.congress,
            bill_type = excluded.bill_type,
            number = excluded.number,
            title = excluded.title,
            sponsor_bioguide = excluded.sponsor_bioguide,
            introduced_date = excluded.introduced_date,
            latest_action_date = excluded.latest_action_date,
            latest_action_text = excluded.latest_action_text,
            policy_area = excluded.policy_area,
            url = excluded.url,
            text_versions = excluded.text_versions,
            cosponsor_count = excluded.cosponsor_count,
            updated_at = NOW()
        """,
        [
            b.bill_id, b.congress, b.bill_type, b.number, b.title,
            b.sponsor_bioguide, b.introduced_date, b.latest_action_date,
            b.latest_action_text, b.policy_area, b.url,
            json.dumps(b.text_versions), b.cosponsor_count,
        ],
    )


def get(store: Store, bill_id: str) -> Optional[Bill]:
    ensure_schema(store)
    row = store.conn.execute(
        """
        SELECT bill_id, congress, bill_type, number, title, sponsor_bioguide,
               introduced_date, latest_action_date, latest_action_text, policy_area,
               url, text_versions, cosponsor_count
        FROM bills WHERE bill_id = ?
        """,
        [bill_id],
    ).fetchone()
    if not row:
        return None
    return Bill(
        bill_id=row[0], congress=row[1], bill_type=row[2], number=row[3],
        title=row[4] or "", sponsor_bioguide=row[5],
        introduced_date=row[6], latest_action_date=row[7],
        latest_action_text=row[8] or "", policy_area=row[9] or "",
        url=row[10] or "",
        text_versions=json.loads(row[11]) if row[11] else [],
        cosponsor_count=int(row[12] or 0),
    )


def recent(store: Store, limit: int = 25) -> list[Bill]:
    ensure_schema(store)
    rows = store.conn.execute(
        """
        SELECT bill_id, congress, bill_type, number, title, sponsor_bioguide,
               introduced_date, latest_action_date, latest_action_text, policy_area,
               url, text_versions, cosponsor_count
        FROM bills
        ORDER BY COALESCE(latest_action_date, introduced_date) DESC NULLS LAST
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    return [
        Bill(
            bill_id=r[0], congress=r[1], bill_type=r[2], number=r[3],
            title=r[4] or "", sponsor_bioguide=r[5],
            introduced_date=r[6], latest_action_date=r[7],
            latest_action_text=r[8] or "", policy_area=r[9] or "",
            url=r[10] or "",
            text_versions=json.loads(r[11]) if r[11] else [],
            cosponsor_count=int(r[12] or 0),
        )
        for r in rows
    ]


# ---------------------------------------------------------------------------
# legis_num → bill_id parser
# ---------------------------------------------------------------------------
# House clerk roll-call XML emits legis_num strings like:
#   "H R 1234"        → hr 1234
#   "H J RES 5"       → hjres 5
#   "H CON RES 5"     → hconres 5
#   "H RES 5"         → hres 5
#   "S 1234"          → s 1234
#   "S J RES 5"       → sjres 5
#   "S CON RES 5"     → sconres 5
#   "S RES 5"         → sres 5
# Returns (bill_type, number) or None if unparseable.

_TYPE_MAP = {
    ("H", "R"): "hr",
    ("H", "JRES"): "hjres",
    ("H", "J", "RES"): "hjres",
    ("H", "CONRES"): "hconres",
    ("H", "CON", "RES"): "hconres",
    ("H", "RES"): "hres",
    ("S",): "s",
    ("S", "JRES"): "sjres",
    ("S", "J", "RES"): "sjres",
    ("S", "CONRES"): "sconres",
    ("S", "CON", "RES"): "sconres",
    ("S", "RES"): "sres",
}


def parse_legis_num(legis_num: str) -> Optional[tuple[str, int]]:
    if not legis_num:
        return None
    parts = legis_num.upper().replace(".", "").split()
    if not parts:
        return None
    # Last part should be the number
    try:
        number = int(parts[-1])
    except ValueError:
        return None
    prefix = tuple(parts[:-1])
    bill_type = _TYPE_MAP.get(prefix)
    if not bill_type:
        return None
    return bill_type, number


def make_bill_id(congress: int | str, legis_num: str) -> Optional[str]:
    parsed = parse_legis_num(legis_num)
    if not parsed:
        return None
    bill_type, number = parsed
    return f"{congress}:{bill_type}:{number}"


def url_for(bill_id: str) -> str:
    """Path component for a bill (URL-safe, slash-separated)."""
    parts = bill_id.split(":")
    if len(parts) != 3:
        return ""
    return f"/bill/{parts[0]}/{parts[1]}/{parts[2]}"
