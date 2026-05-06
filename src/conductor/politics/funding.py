"""Per-legislator funding totals from OpenFEC.

Entity-state table (one row per (bioguide, cycle)), not an event stream.
Pulled via async sync — see funding_sync.py.

Cycles are 2-year windows ending on the federal general election (even years).
2025–2026 cycle = "cycle 2026". A House member is in every cycle (2-yr terms);
a Senator's cycle activity depends on their class but FEC files quarterly
regardless, so totals exist for off-cycle senators too — usually small.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date
from typing import Optional

from conductor.store import Store

logger = logging.getLogger(__name__)

FUNDING_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS funding_totals (
    bioguide_id              VARCHAR NOT NULL,
    cycle                    INTEGER NOT NULL,
    fec_id                   VARCHAR,
    receipts                 DECIMAL,
    disbursements            DECIMAL,
    cash_on_hand             DECIMAL,
    debts                    DECIMAL,
    individual_contributions DECIMAL,
    pac_contributions        DECIMAL,
    party_contributions      DECIMAL,
    candidate_contribution   DECIMAL,
    coverage_start           DATE,
    coverage_end             DATE,
    last_report_label        VARCHAR,
    fetched_at               TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (bioguide_id, cycle)
);
CREATE INDEX IF NOT EXISTS idx_funding_cycle ON funding_totals (cycle);
CREATE INDEX IF NOT EXISTS idx_funding_receipts ON funding_totals (receipts);

CREATE TABLE IF NOT EXISTS fec_id_resolutions (
    bioguide_id  VARCHAR NOT NULL,
    cycle        INTEGER NOT NULL,
    fec_id       VARCHAR,         -- NULL = searched, no match (negative cache)
    source       VARCHAR,         -- 'primary' | 'search' | 'manual'
    resolved_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (bioguide_id, cycle)
);
"""


def get_resolution(store: Store, bioguide: str, cycle: int) -> tuple[bool, Optional[str]]:
    """Look up cached fec_id resolution for (bioguide, cycle).
    Returns (is_cached, fec_id_or_none). is_cached=False means never searched."""
    ensure_schema(store)
    row = store.conn.execute(
        "SELECT fec_id FROM fec_id_resolutions WHERE bioguide_id = ? AND cycle = ?",
        [bioguide, cycle],
    ).fetchone()
    if row is None:
        return False, None
    return True, row[0]


def put_resolution(
    store: Store, bioguide: str, cycle: int, fec_id: Optional[str], source: str,
) -> None:
    ensure_schema(store)
    store.conn.execute(
        """
        INSERT INTO fec_id_resolutions (bioguide_id, cycle, fec_id, source, resolved_at)
        VALUES (?, ?, ?, ?, NOW())
        ON CONFLICT (bioguide_id, cycle) DO UPDATE SET
            fec_id = excluded.fec_id,
            source = excluded.source,
            resolved_at = NOW()
        """,
        [bioguide, cycle, fec_id, source],
    )


@dataclass
class FundingTotal:
    bioguide_id: str
    cycle: int
    fec_id: Optional[str]
    receipts: float
    disbursements: float
    cash_on_hand: Optional[float]
    debts: Optional[float]
    individual_contributions: Optional[float]
    pac_contributions: Optional[float]
    party_contributions: Optional[float]
    candidate_contribution: Optional[float]
    coverage_start: Optional[date]
    coverage_end: Optional[date]
    last_report_label: Optional[str]


def ensure_schema(store: Store) -> None:
    if getattr(store, "read_only", False):
        return
    store.conn.execute(FUNDING_SCHEMA_SQL)


def upsert(store: Store, t: FundingTotal) -> None:
    ensure_schema(store)
    store.conn.execute(
        """
        INSERT INTO funding_totals
            (bioguide_id, cycle, fec_id, receipts, disbursements, cash_on_hand,
             debts, individual_contributions, pac_contributions, party_contributions,
             candidate_contribution, coverage_start, coverage_end, last_report_label,
             fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NOW())
        ON CONFLICT (bioguide_id, cycle) DO UPDATE SET
            fec_id = excluded.fec_id,
            receipts = excluded.receipts,
            disbursements = excluded.disbursements,
            cash_on_hand = excluded.cash_on_hand,
            debts = excluded.debts,
            individual_contributions = excluded.individual_contributions,
            pac_contributions = excluded.pac_contributions,
            party_contributions = excluded.party_contributions,
            candidate_contribution = excluded.candidate_contribution,
            coverage_start = excluded.coverage_start,
            coverage_end = excluded.coverage_end,
            last_report_label = excluded.last_report_label,
            fetched_at = NOW()
        """,
        [
            t.bioguide_id, t.cycle, t.fec_id, t.receipts, t.disbursements,
            t.cash_on_hand, t.debts, t.individual_contributions,
            t.pac_contributions, t.party_contributions, t.candidate_contribution,
            t.coverage_start, t.coverage_end, t.last_report_label,
        ],
    )


def for_member(store: Store, bioguide: str) -> list[FundingTotal]:
    """All cycles we've stored for this member, newest first."""
    ensure_schema(store)
    rows = store.conn.execute(
        """
        SELECT bioguide_id, cycle, fec_id, receipts, disbursements, cash_on_hand,
               debts, individual_contributions, pac_contributions, party_contributions,
               candidate_contribution, coverage_start, coverage_end, last_report_label
        FROM funding_totals
        WHERE bioguide_id = ?
        ORDER BY cycle DESC
        """,
        [bioguide],
    ).fetchall()
    return [
        FundingTotal(
            bioguide_id=r[0], cycle=int(r[1]), fec_id=r[2],
            receipts=float(r[3] or 0), disbursements=float(r[4] or 0),
            cash_on_hand=float(r[5]) if r[5] is not None else None,
            debts=float(r[6]) if r[6] is not None else None,
            individual_contributions=float(r[7]) if r[7] is not None else None,
            pac_contributions=float(r[8]) if r[8] is not None else None,
            party_contributions=float(r[9]) if r[9] is not None else None,
            candidate_contribution=float(r[10]) if r[10] is not None else None,
            coverage_start=r[11], coverage_end=r[12],
            last_report_label=r[13],
        )
        for r in rows
    ]


def latest(store: Store, bioguide: str) -> Optional[FundingTotal]:
    rows = for_member(store, bioguide)
    return rows[0] if rows else None
