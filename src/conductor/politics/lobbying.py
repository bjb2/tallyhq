"""LDA filing entity + helpers.

Lobbying Disclosure Act filings are first-class entities — keyed by
filing_uuid (Senate LDA assigns globally-unique UUIDs). Activities (free
text) are scanned for bill references; resolved bill_ids land in
`bill_refs` JSON.

filing_uuid is the canonical id (UUID4 string).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from conductor.store import Store

LDA_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS lda_filings (
    filing_uuid       VARCHAR PRIMARY KEY,
    filing_year       INTEGER,
    filing_period     VARCHAR,
    dt_posted         TIMESTAMPTZ,
    income            DECIMAL,
    expenses          DECIMAL,
    registrant_id     VARCHAR,
    registrant_name   VARCHAR,
    client_id         VARCHAR,
    client_name       VARCHAR,
    activity_count    INTEGER,
    issue_codes       JSON,
    bill_refs         JSON,
    raw_url           VARCHAR,
    updated_at        TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_lda_year_period ON lda_filings (filing_year, filing_period);
CREATE INDEX IF NOT EXISTS idx_lda_registrant ON lda_filings (registrant_id);
CREATE INDEX IF NOT EXISTS idx_lda_client ON lda_filings (client_id);
"""


@dataclass
class LdaFiling:
    filing_uuid: str
    filing_year: int
    filing_period: str
    dt_posted: Optional[datetime]
    income: Optional[float]
    expenses: Optional[float]
    registrant_id: Optional[str]
    registrant_name: str
    client_id: Optional[str]
    client_name: str
    activity_count: int
    issue_codes: list[str] = field(default_factory=list)
    bill_refs: list[str] = field(default_factory=list)
    raw_url: str = ""


def ensure_schema(store: Store) -> None:
    store.conn.execute(LDA_SCHEMA_SQL)


def upsert(store: Store, f: LdaFiling) -> None:
    ensure_schema(store)
    store.conn.execute(
        """
        INSERT INTO lda_filings
            (filing_uuid, filing_year, filing_period, dt_posted, income, expenses,
             registrant_id, registrant_name, client_id, client_name,
             activity_count, issue_codes, bill_refs, raw_url, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NOW())
        ON CONFLICT (filing_uuid) DO UPDATE SET
            filing_year = excluded.filing_year,
            filing_period = excluded.filing_period,
            dt_posted = excluded.dt_posted,
            income = excluded.income,
            expenses = excluded.expenses,
            registrant_id = excluded.registrant_id,
            registrant_name = excluded.registrant_name,
            client_id = excluded.client_id,
            client_name = excluded.client_name,
            activity_count = excluded.activity_count,
            issue_codes = excluded.issue_codes,
            bill_refs = excluded.bill_refs,
            raw_url = excluded.raw_url,
            updated_at = NOW()
        """,
        [
            f.filing_uuid, f.filing_year, f.filing_period, f.dt_posted,
            f.income, f.expenses,
            f.registrant_id, f.registrant_name, f.client_id, f.client_name,
            f.activity_count,
            json.dumps(f.issue_codes), json.dumps(f.bill_refs),
            f.raw_url,
        ],
    )


def for_bill(store: Store, bill_id: str, limit: int = 50) -> list[dict]:
    """Recent filings whose bill_refs contains the given bill_id."""
    ensure_schema(store)
    rows = store.conn.execute(
        """
        SELECT filing_uuid, filing_year, filing_period, dt_posted,
               income, registrant_name, client_name, issue_codes, bill_refs, raw_url
        FROM lda_filings
        WHERE list_contains(CAST(bill_refs AS JSON[]), ?)
           OR bill_refs::VARCHAR LIKE ?
        ORDER BY dt_posted DESC NULLS LAST
        LIMIT ?
        """,
        [bill_id, f'%"{bill_id}"%', limit],
    ).fetchall()
    return [
        {
            "filing_uuid": r[0],
            "filing_year": r[1],
            "filing_period": r[2],
            "dt_posted": r[3],
            "income": float(r[4]) if r[4] is not None else None,
            "registrant_name": r[5],
            "client_name": r[6],
            "issue_codes": json.loads(r[7]) if r[7] else [],
            "bill_refs": json.loads(r[8]) if r[8] else [],
            "raw_url": r[9],
        }
        for r in rows
    ]


# ---------------------------------------------------------------------------
# Bill reference extraction
# ---------------------------------------------------------------------------
# Free-text patterns we accept inside lobbying_activities[].description:
#   "H.R. 1234", "HR1234", "H R 1234"
#   "S. 567", "S 567"
#   "H.J.Res. 5", "H J Res 5", "HJRes 5"
#   "S.J.Res. 5"
#   "H.Con.Res. 5", "H Con Res 5"
#   "S.Con.Res. 5"
#   "H.Res. 5"
#   "S.Res. 5"
#
# Regex order matters — match longer/more-specific first.

_BILL_PATTERNS = [
    (re.compile(r"(?i)\bH\.?\s?J\.?\s?Res\.?\s*(\d{1,5})\b"), "hjres"),
    (re.compile(r"(?i)\bS\.?\s?J\.?\s?Res\.?\s*(\d{1,5})\b"), "sjres"),
    (re.compile(r"(?i)\bH\.?\s?Con\.?\s?Res\.?\s*(\d{1,5})\b"), "hconres"),
    (re.compile(r"(?i)\bS\.?\s?Con\.?\s?Res\.?\s*(\d{1,5})\b"), "sconres"),
    (re.compile(r"(?i)\bH\.?\s?Res\.?\s*(\d{1,5})\b"), "hres"),
    (re.compile(r"(?i)\bS\.?\s?Res\.?\s*(\d{1,5})\b"), "sres"),
    (re.compile(r"(?i)\bH\.?\s?R\.?\s*(\d{1,5})\b"), "hr"),
    # plain S followed by digits — must be word-bounded and not preceded by letters
    (re.compile(r"(?i)(?:^|[^A-Za-z.])S\.?\s*(\d{1,5})\b"), "s"),
]


def congress_for_year(year: int) -> int:
    # 119th = 2025-2026, 118th = 2023-2024, ...
    return 119 + (year - 2025) // 2


def extract_bill_refs(text: str, year: int) -> list[str]:
    if not text:
        return []
    congress = congress_for_year(year)
    found: set[str] = set()
    # Walk in pattern order, blanking matches as we go so an "H.J.Res. 5" doesn't
    # also match the tail "Res. 5" or the head "H.R" with stray digits.
    remaining = text
    for pat, btype in _BILL_PATTERNS:
        def _sub(m, _bt=btype):
            num = m.group(1)
            try:
                n = int(num)
            except ValueError:
                return " " * len(m.group(0))
            if n <= 0 or n > 99999:
                return " " * len(m.group(0))
            found.add(f"{congress}:{_bt}:{n}")
            return " " * len(m.group(0))
        remaining = pat.sub(_sub, remaining)
    return sorted(found)
