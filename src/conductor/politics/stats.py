"""Derived per-legislator stats for profile pages.

Cheap SQL aggregates over the events table. Computed on-demand; no caching
yet (DuckDB on 34k rows is sub-millisecond).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from conductor.store import Store


@dataclass
class LegislatorStats:
    total_votes: int
    yea_count: int
    nay_count: int
    missed_count: int
    party_line_count: int
    break_count: int
    cosponsorships: int
    bills_sponsored: int
    floor_speeches: int
    last_active: date | None

    @property
    def break_rate(self) -> float:
        decided = self.party_line_count + self.break_count
        return (self.break_count / decided) if decided else 0.0


def compute(store: Store, entity_id: str) -> LegislatorStats:
    row = store.conn.execute(
        """
        SELECT
            SUM(CASE WHEN event_type = 'vote.cast' AND json_extract_string(payload, '$.position') = 'Yea'  THEN 1 ELSE 0 END) AS yea,
            SUM(CASE WHEN event_type = 'vote.cast' AND json_extract_string(payload, '$.position') = 'Nay'  THEN 1 ELSE 0 END) AS nay,
            SUM(CASE WHEN event_type = 'vote.missed' THEN 1 ELSE 0 END) AS missed,
            SUM(CASE WHEN event_type = 'vote.cast' AND json_extract_string(payload, '$.party_line') = 'true'  THEN 1 ELSE 0 END) AS pl,
            SUM(CASE WHEN event_type = 'vote.cast' AND json_extract_string(payload, '$.party_line') = 'false' THEN 1 ELSE 0 END) AS brk,
            SUM(CASE WHEN event_type = 'bill.cosponsored' THEN 1 ELSE 0 END) AS cos,
            SUM(CASE WHEN event_type = 'bill.sponsored' THEN 1 ELSE 0 END) AS spn,
            SUM(CASE WHEN event_type = 'floor.speech' THEN 1 ELSE 0 END) AS spch,
            CAST(MAX(occurred_at) AS DATE) AS last_active
        FROM events
        WHERE entity_id = ?
        """,
        [entity_id],
    ).fetchone()

    yea, nay, missed, pl, brk, cos, spn, spch, last = (r or 0 for r in row)
    last_active = row[8] if row and row[8] else None
    return LegislatorStats(
        total_votes=int((yea or 0) + (nay or 0)),
        yea_count=int(yea or 0),
        nay_count=int(nay or 0),
        missed_count=int(missed or 0),
        party_line_count=int(pl or 0),
        break_count=int(brk or 0),
        cosponsorships=int(cos or 0),
        bills_sponsored=int(spn or 0),
        floor_speeches=int(spch or 0),
        last_active=last_active,
    )
