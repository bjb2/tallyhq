"""Landing-page aggregations — story-style overview of the dataset.

Computed on-demand from the events table. DuckDB is fast enough that
caching isn't worth it yet at 30k–500k rows.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Optional

from conductor.aggregations.activity_grid import grid as compute_grid, GridRow
from conductor.politics import entities as ent_mod
from conductor.politics.entities import FederalEntity
from conductor.store import Store


@dataclass
class CongressTotals:
    members: int
    house_members: int
    senate_members: int
    voting_house: int
    delegates: int
    vacancies: int
    total_events: int
    total_votes: int
    total_breaks: int
    earliest: Optional[date]
    latest: Optional[date]
    rollcalls_house: int
    rollcalls_senate: int


@dataclass
class PulseStats:
    busiest_days: list[tuple[date, int]]   # (day, event_count) top 5
    by_dow: list[tuple[str, int]]          # ("Mon", count) Mon..Sun
    active_days: int                       # days with any activity in window
    window_days: int                       # total days in window
    peak_count: int


@dataclass
class RankedRow:
    entity: FederalEntity
    value: float
    secondary: str = ""


def totals(store: Store) -> CongressTotals:
    members = ent_mod.list_all(store)
    # Non-voting delegates serve from federal territories.
    # (district == 0 is also "at-large", which can be a voting seat in
    # single-district states like AK/MT/etc., so it's not a reliable signal.)
    territory_states = {"DC", "PR", "VI", "GU", "AS", "MP"}
    delegate_count = sum(
        1 for m in members
        if m.chamber == "house" and m.state in territory_states
    )
    house_total = sum(1 for m in members if m.chamber == "house")
    voting_house = house_total - delegate_count
    vacancies = max(0, 435 - voting_house)

    rows = store.conn.execute(
        """
        SELECT
            COUNT(*) AS evts,
            SUM(CASE WHEN event_type = 'vote.cast' THEN 1 ELSE 0 END) AS votes,
            SUM(CASE WHEN event_type = 'vote.cast'
                     AND json_extract_string(payload, '$.party_line') = 'false'
                     THEN 1 ELSE 0 END) AS breaks,
            CAST(MIN(occurred_at) AS DATE) AS earliest,
            CAST(MAX(occurred_at) AS DATE) AS latest
        FROM events
        WHERE entity_id LIKE 'bioguide:%'
        """
    ).fetchone()
    rollcalls_house = store.conn.execute(
        """
        SELECT COUNT(DISTINCT json_extract_string(payload, '$.rollcall_num'))
        FROM events
        WHERE source = 'congress_rollcalls'
        """
    ).fetchone()
    rollcalls_senate = store.conn.execute(
        """
        SELECT COUNT(DISTINCT (
          json_extract_string(payload, '$.session') || ':' ||
          json_extract_string(payload, '$.rollcall_num')
        ))
        FROM events
        WHERE source = 'senate_rollcalls'
        """
    ).fetchone()
    return CongressTotals(
        members=len(members),
        house_members=house_total,
        senate_members=sum(1 for m in members if m.chamber == "senate"),
        voting_house=voting_house,
        delegates=delegate_count,
        vacancies=vacancies,
        total_events=int(rows[0] or 0),
        total_votes=int(rows[1] or 0),
        total_breaks=int(rows[2] or 0),
        earliest=rows[3],
        latest=rows[4],
        rollcalls_house=int(rollcalls_house[0] or 0),
        rollcalls_senate=int(rollcalls_senate[0] or 0),
    )


def pulse_stats(store: Store, days: int = 180) -> PulseStats:
    end = date.today()
    start = end - timedelta(days=days)
    rows = store.conn.execute(
        """
        SELECT
            CAST(occurred_at AT TIME ZONE 'UTC' AS DATE) AS day,
            COUNT(*) AS cnt
        FROM events
        WHERE entity_id LIKE 'bioguide:%'
          AND occurred_at >= ?
          AND occurred_at < ?
        GROUP BY day
        ORDER BY cnt DESC
        """,
        [start, end + timedelta(days=1)],
    ).fetchall()
    if not rows:
        return PulseStats([], [], 0, days, 0)
    busiest = [(r[0], int(r[1])) for r in rows[:5]]
    # By day of week
    dow_rows = store.conn.execute(
        """
        SELECT
            EXTRACT(dow FROM occurred_at AT TIME ZONE 'UTC') AS dow,
            COUNT(*) AS cnt
        FROM events
        WHERE entity_id LIKE 'bioguide:%'
          AND occurred_at >= ?
          AND occurred_at < ?
        GROUP BY dow
        ORDER BY dow
        """,
        [start, end + timedelta(days=1)],
    ).fetchall()
    dow_map = {int(r[0]): int(r[1]) for r in dow_rows}
    labels = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]
    by_dow = [(labels[i], dow_map.get(i, 0)) for i in range(7)]
    return PulseStats(
        busiest_days=busiest,
        by_dow=by_dow,
        active_days=len(rows),
        window_days=days,
        peak_count=int(rows[0][1]) if rows else 0,
    )


def aggregate_grid(store: Store, days: int = 180) -> GridRow:
    """Sum-of-intensity grid across all legislators — congress as one entity."""
    end = date.today()
    start = end - timedelta(days=days)
    rows = store.conn.execute(
        """
        SELECT
            CAST(occurred_at AT TIME ZONE 'UTC' AS DATE) AS day,
            COUNT(*) AS cnt
        FROM events
        WHERE entity_id LIKE 'bioguide:%'
          AND occurred_at >= ?
          AND occurred_at < ?
        GROUP BY day
        ORDER BY day
        """,
        [start, end + timedelta(days=1)],
    ).fetchall()
    # Synthesize a GridRow with normalized intensity bands
    from conductor.aggregations.activity_grid import GridCell, _quantile_bands, _band
    by_day = {r[0]: int(r[1]) for r in rows}
    days_list = []
    cur = start
    while cur <= end:
        days_list.append(cur)
        cur += timedelta(days=1)
    intensities = [float(by_day.get(d, 0)) for d in days_list]
    thresholds = _quantile_bands(intensities)
    cells = [
        GridCell(day=d, intensity=float(by_day.get(d, 0)),
                 count=by_day.get(d, 0), band=_band(float(by_day.get(d, 0)), thresholds))
        for d in days_list
    ]
    return GridRow(entity_id="congress:all", start=start, end=end,
                   cells=cells, total=sum(c.intensity for c in cells))


def _ranked(
    store: Store, sql: str, params: list, *, secondary_fmt: str | None = None
) -> list[RankedRow]:
    rows = store.conn.execute(sql, params).fetchall()
    out: list[RankedRow] = []
    for entity_id, value, *extra in rows:
        bg = entity_id.split(":", 1)[1]
        e = ent_mod.get(store, bg)
        if not e:
            continue
        sec = ""
        if secondary_fmt and extra:
            sec = secondary_fmt.format(*extra)
        out.append(RankedRow(entity=e, value=float(value or 0), secondary=sec))
    return out


def most_active(store: Store, days: int = 180, limit: int = 10) -> list[RankedRow]:
    cutoff = date.today() - timedelta(days=days)
    return _ranked(
        store,
        """
        SELECT entity_id,
               COUNT(*) AS evts
        FROM events
        WHERE entity_id LIKE 'bioguide:%'
          AND occurred_at >= ?
        GROUP BY entity_id
        ORDER BY evts DESC
        LIMIT ?
        """,
        [cutoff, limit],
    )


def least_active(store: Store, days: int = 180, limit: int = 10) -> list[RankedRow]:
    """Members who appear in the events table but with the fewest events."""
    cutoff = date.today() - timedelta(days=days)
    return _ranked(
        store,
        """
        SELECT entity_id, COUNT(*) AS evts
        FROM events
        WHERE entity_id LIKE 'bioguide:%' AND occurred_at >= ?
        GROUP BY entity_id
        HAVING evts > 0
        ORDER BY evts ASC
        LIMIT ?
        """,
        [cutoff, limit],
    )


def most_absent(store: Store, days: int = 180, limit: int = 10) -> list[RankedRow]:
    cutoff = date.today() - timedelta(days=days)
    return _ranked(
        store,
        """
        SELECT entity_id,
               SUM(CASE WHEN event_type = 'vote.missed' THEN 1 ELSE 0 END) AS missed,
               SUM(CASE WHEN event_type IN ('vote.cast','vote.missed') THEN 1 ELSE 0 END) AS total
        FROM events
        WHERE entity_id LIKE 'bioguide:%' AND occurred_at >= ?
        GROUP BY entity_id
        HAVING total > 10
        ORDER BY missed DESC
        LIMIT ?
        """,
        [cutoff, limit],
        secondary_fmt="of {0} roll-calls",
    )


def biggest_breakers(store: Store, days: int = 180, min_votes: int = 30, limit: int = 10) -> list[RankedRow]:
    cutoff = date.today() - timedelta(days=days)
    return _ranked(
        store,
        """
        SELECT entity_id,
               100.0 * SUM(CASE WHEN json_extract_string(payload, '$.party_line') = 'false' THEN 1 ELSE 0 END)
                       / NULLIF(SUM(CASE WHEN event_type = 'vote.cast' THEN 1 ELSE 0 END), 0) AS rate,
               SUM(CASE WHEN json_extract_string(payload, '$.party_line') = 'false' THEN 1 ELSE 0 END) AS breaks,
               SUM(CASE WHEN event_type = 'vote.cast' THEN 1 ELSE 0 END) AS votes
        FROM events
        WHERE entity_id LIKE 'bioguide:%' AND occurred_at >= ?
        GROUP BY entity_id
        HAVING votes >= ?
        ORDER BY rate DESC
        LIMIT ?
        """,
        [cutoff, min_votes, limit],
        secondary_fmt="{0} breaks of {1} votes",
    )


@dataclass
class RecentBreak:
    when: datetime
    entity: FederalEntity
    legis_num: str
    question: str
    position: str
    url: str


def recent_breaks(store: Store, limit: int = 12) -> list[RecentBreak]:
    rows = store.conn.execute(
        """
        SELECT occurred_at, entity_id,
               json_extract_string(payload, '$.legis_num'),
               json_extract_string(payload, '$.question'),
               json_extract_string(payload, '$.position'),
               json_extract_string(payload, '$.url')
        FROM events
        WHERE event_type = 'vote.cast'
          AND json_extract_string(payload, '$.party_line') = 'false'
        ORDER BY occurred_at DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    out: list[RecentBreak] = []
    for occ, eid, legis, q, pos, url in rows:
        bg = eid.split(":", 1)[1]
        e = ent_mod.get(store, bg)
        if not e:
            continue
        out.append(RecentBreak(when=occ, entity=e, legis_num=legis or "",
                               question=q or "", position=pos or "", url=url or ""))
    return out


def cohort_grids(store: Store, entities: list[FederalEntity], days: int = 90) -> list[tuple[FederalEntity, GridRow]]:
    end = date.today()
    start = end - timedelta(days=days)
    return [(e, compute_grid(store, e.entity_id, start, end)) for e in entities]


def recent_bills(store: Store, limit: int = 8):
    from conductor.politics import bills as bm
    return bm.recent(store, limit=limit)


def most_cosponsored(store: Store, limit: int = 5):
    from conductor.politics import bills as bm
    bm.ensure_schema(store)
    rows = store.conn.execute(
        """
        SELECT bill_id, congress, bill_type, number, title, sponsor_bioguide,
               introduced_date, latest_action_date, latest_action_text, policy_area,
               url, text_versions, cosponsor_count
        FROM bills
        ORDER BY cosponsor_count DESC
        LIMIT ?
        """,
        [limit],
    ).fetchall()
    import json
    return [
        bm.Bill(
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
