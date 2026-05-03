"""Read-side helpers for bill detail page.

Joins bills + events + legislators to assemble:
  - sponsor info
  - cosponsors list (with photos)
  - roll-call votes on this bill (grouped by roll-call number)
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from conductor.politics import bills as bills_mod, entities as ent_mod
from conductor.politics.bills import parse_legis_num
from conductor.politics.entities import FederalEntity
from conductor.store import Store


@dataclass
class CosponsorRow:
    entity: FederalEntity
    sponsorship_date: Optional[str]
    is_original: bool


@dataclass
class RollCallTally:
    rollcall_num: int
    chamber: str
    when: datetime
    question: str
    result: str
    yea: int = 0
    nay: int = 0
    missed: int = 0
    by_party: dict[str, dict[str, int]] = None  # party -> {Yea, Nay, Missed}
    url: str = ""

    def __post_init__(self):
        if self.by_party is None:
            self.by_party = defaultdict(lambda: {"Yea": 0, "Nay": 0, "Missed": 0})


def cosponsors(store: Store, bill_id: str) -> list[CosponsorRow]:
    rows = store.conn.execute(
        """
        SELECT entity_id, payload, occurred_at
        FROM events
        WHERE event_type = 'bill.cosponsored'
          AND json_extract_string(payload, '$.bill_id') = ?
        ORDER BY occurred_at ASC
        """,
        [bill_id],
    ).fetchall()
    out: list[CosponsorRow] = []
    for entity_id, payload, occurred_at in rows:
        p = json.loads(payload) if isinstance(payload, str) else payload
        bg = entity_id.split(":", 1)[1]
        e = ent_mod.get(store, bg)
        if not e:
            continue
        out.append(CosponsorRow(
            entity=e,
            sponsorship_date=occurred_at.isoformat() if occurred_at else None,
            is_original=bool(p.get("is_original")),
        ))
    return out


def sponsor(store: Store, b: bills_mod.Bill) -> Optional[FederalEntity]:
    if not b.sponsor_bioguide:
        return None
    return ent_mod.get(store, b.sponsor_bioguide)


def rollcall_tallies(store: Store, bill_id: str) -> list[RollCallTally]:
    """Find roll-call votes whose legis_num parses to this bill, group by
    roll-call number, tally party-by-party.
    """
    parts = bill_id.split(":")
    if len(parts) != 3:
        return []
    target_congress, target_type, target_num_str = parts
    try:
        target_num = int(target_num_str)
    except ValueError:
        return []

    rows = store.conn.execute(
        """
        SELECT entity_id, event_type, occurred_at, payload
        FROM events
        WHERE event_type IN ('vote.cast', 'vote.missed')
          AND json_extract_string(payload, '$.congress') = ?
        """,
        [str(target_congress)],
    ).fetchall()

    tallies: dict[int, RollCallTally] = {}
    for entity_id, event_type, occurred_at, payload in rows:
        p = json.loads(payload) if isinstance(payload, str) else payload
        legis = p.get("legis_num") or ""
        parsed = parse_legis_num(legis)
        if not parsed or parsed[0] != target_type or parsed[1] != target_num:
            continue
        roll = int(p.get("rollcall_num") or 0)
        t = tallies.get(roll)
        if t is None:
            t = RollCallTally(
                rollcall_num=roll,
                chamber=p.get("chamber") or "house",
                when=occurred_at,
                question=p.get("question") or "",
                result=p.get("result") or "",
                url=p.get("url") or "",
            )
            tallies[roll] = t

        party = p.get("party") or ""
        position = p.get("position") or ""
        if event_type == "vote.missed":
            t.missed += 1
            t.by_party[party]["Missed"] += 1
        elif position in ("Yea", "Nay"):
            if position == "Yea":
                t.yea += 1
            else:
                t.nay += 1
            t.by_party[party][position] += 1

    return sorted(tallies.values(), key=lambda x: x.rollcall_num)
