"""Activity-grid primitive — entity-by-day intensity matrix.

Substrate-level. Vertical-agnostic. Used by politics for legislator dot-grids,
extensible to any per-entity event stream (athlete game-logs, dev commits,
agency enforcement actions, etc.).

Rendering is separate (see vertical-specific render modules); this module
only computes the (day, intensity) rows and quantile band assignment.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta

from conductor.store import Store


@dataclass
class GridCell:
    day: date
    intensity: float
    count: int
    band: int  # 0..4, GitHub-style

    def to_dict(self) -> dict:
        return {
            "day": self.day.isoformat(),
            "intensity": self.intensity,
            "count": self.count,
            "band": self.band,
        }


@dataclass
class GridRow:
    entity_id: str
    start: date
    end: date
    cells: list[GridCell]
    total: float

    def to_dict(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "total": self.total,
            "cells": [c.to_dict() for c in self.cells],
        }


def _quantile_bands(values: list[float]) -> list[float]:
    """Return 4 thresholds splitting positive values into 5 bands (0..4).

    Band 0 = no activity. Bands 1..4 = quartiles of positive intensity.
    """
    pos = sorted(v for v in values if v > 0)
    if not pos:
        return [0.0, 0.0, 0.0, 0.0]
    n = len(pos)
    return [
        pos[int(n * 0.25)],
        pos[int(n * 0.50)],
        pos[int(n * 0.75)],
        pos[-1],
    ]


def _band(intensity: float, thresholds: list[float]) -> int:
    if intensity <= 0:
        return 0
    if intensity <= thresholds[0]:
        return 1
    if intensity <= thresholds[1]:
        return 2
    if intensity <= thresholds[2]:
        return 3
    return 4


DEFAULT_WEIGHTS = {
    "vote.cast": 1.0,
    "vote.cast.break": 3.0,
    "vote.missed": -0.5,
    "bill.sponsored": 5.0,
    "bill.cosponsored": 1.0,
    "committee.markup_vote": 2.0,
    "committee.hearing_attended": 1.0,
    "floor.speech": 2.0,
    "floor.amendment_offered": 3.0,
    "letter.signed": 1.0,
    "press.release": 0.5,
    "town_hall.held": 1.0,
    "ethics.filing": 2.0,
    "cr.statement": 1.0,
    "discharge_petition.signed": 4.0,
}


def _weight_case_sql(weights: dict[str, float]) -> str:
    """Build a CASE expression scoring each event row."""
    branches = "\n".join(
        f"        WHEN event_type = '{k}' THEN {v}" for k, v in weights.items()
    )
    return f"""
    CASE
{branches}
        ELSE 0.0
    END
    """


def grid(
    store: Store,
    entity_id: str,
    start: date,
    end: date,
    weights: dict[str, float] | None = None,
    cohort_thresholds: list[float] | None = None,
) -> GridRow:
    """Compute activity-grid row for a single entity.

    cohort_thresholds, when provided, lets multiple entities share a band scale
    (so a workhorse doesn't blow out the comparison).
    """
    weights = weights or DEFAULT_WEIGHTS
    case_sql = _weight_case_sql(weights)

    # Vote-break upgrade: vote.cast where payload.party_line = false counts as vote.cast.break
    rows = store.conn.execute(
        f"""
        SELECT
            CAST(occurred_at AT TIME ZONE 'UTC' AS DATE) AS day,
            SUM(
                CASE
                    WHEN event_type = 'vote.cast'
                         AND COALESCE(json_extract_string(payload, '$.party_line'), 'true') = 'false'
                    THEN {weights.get('vote.cast.break', 3.0)}
                    ELSE {case_sql}
                END
            ) AS intensity,
            COUNT(*) AS cnt
        FROM events
        WHERE entity_id = ?
          AND occurred_at >= ?
          AND occurred_at < ?
        GROUP BY day
        ORDER BY day
        """,
        [entity_id, start, end + timedelta(days=1)],
    ).fetchall()

    by_day = {r[0]: (float(r[1] or 0), int(r[2] or 0)) for r in rows}

    # Fill missing days with zeros
    days: list[date] = []
    cur = start
    while cur <= end:
        days.append(cur)
        cur += timedelta(days=1)

    intensities = [by_day.get(d, (0.0, 0))[0] for d in days]
    thresholds = cohort_thresholds or _quantile_bands(intensities)

    cells = [
        GridCell(
            day=d,
            intensity=by_day.get(d, (0.0, 0))[0],
            count=by_day.get(d, (0.0, 0))[1],
            band=_band(by_day.get(d, (0.0, 0))[0], thresholds),
        )
        for d in days
    ]
    total = sum(c.intensity for c in cells)
    return GridRow(entity_id=entity_id, start=start, end=end, cells=cells, total=total)


def cohort_grid(
    store: Store,
    entity_ids: list[str],
    start: date,
    end: date,
    weights: dict[str, float] | None = None,
) -> list[GridRow]:
    """Compute grids for a cohort with shared band thresholds across entities."""
    weights = weights or DEFAULT_WEIGHTS
    # First pass — get raw intensities to derive cohort thresholds
    pre = [grid(store, eid, start, end, weights=weights, cohort_thresholds=[0, 0, 0, 0]) for eid in entity_ids]
    pooled: list[float] = [c.intensity for r in pre for c in r.cells]
    thresholds = _quantile_bands(pooled)
    return [
        grid(store, eid, start, end, weights=weights, cohort_thresholds=thresholds)
        for eid in entity_ids
    ]
