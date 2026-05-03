"""Read-side helpers for roll-call detail page."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from conductor.politics import entities as ent_mod
from conductor.politics.entities import FederalEntity
from conductor.store import Store


@dataclass
class VoteRecord:
    entity: FederalEntity
    position: str               # 'Yea' | 'Nay' | 'Not Voting' | 'Present'
    party: str
    state: str
    party_line: Optional[bool]


@dataclass
class RollcallDetail:
    chamber: str                # 'house' | 'senate'
    congress: int
    session: Optional[int]
    rollcall_num: int
    occurred_at: datetime
    question: str
    result: str
    legis_num: str
    url: str
    votes: list[VoteRecord]
    by_party: dict[str, dict[str, int]] = field(default_factory=dict)
    yea: int = 0
    nay: int = 0
    missed: int = 0
    present: int = 0


def _ensure_party_bucket(d: dict[str, dict[str, int]], party: str) -> dict[str, int]:
    if party not in d:
        d[party] = {"Yea": 0, "Nay": 0, "Not Voting": 0, "Present": 0}
    return d[party]


def get_house_rollcall(store: Store, year: int, num: int) -> Optional[RollcallDetail]:
    rows = store.conn.execute(
        """
        SELECT entity_id, event_type, occurred_at, payload
        FROM events
        WHERE source = 'congress_rollcalls'
          AND CAST(json_extract_string(payload, '$.year') AS INTEGER) = ?
          AND CAST(json_extract_string(payload, '$.rollcall_num') AS INTEGER) = ?
        """,
        [year, num],
    ).fetchall()
    return _build(store, "house", rows)


def get_senate_rollcall(
    store: Store, congress: int, session: int, num: int
) -> Optional[RollcallDetail]:
    rows = store.conn.execute(
        """
        SELECT entity_id, event_type, occurred_at, payload
        FROM events
        WHERE source = 'senate_rollcalls'
          AND CAST(json_extract_string(payload, '$.congress') AS INTEGER) = ?
          AND CAST(json_extract_string(payload, '$.session') AS INTEGER) = ?
          AND CAST(json_extract_string(payload, '$.rollcall_num') AS INTEGER) = ?
        """,
        [congress, session, num],
    ).fetchall()
    return _build(store, "senate", rows)


def _safe_int(v) -> Optional[int]:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        # House publishes ordinal sessions like "2nd" — extract leading digits
        import re
        m = re.match(r"\d+", str(v))
        return int(m.group(0)) if m else None


def _build(store: Store, chamber: str, rows: list) -> Optional[RollcallDetail]:
    if not rows:
        return None
    common = json.loads(rows[0][3]) if isinstance(rows[0][3], str) else rows[0][3]
    detail = RollcallDetail(
        chamber=chamber,
        congress=_safe_int(common.get("congress")) or 0,
        session=_safe_int(common.get("session")),
        rollcall_num=_safe_int(common.get("rollcall_num")) or 0,
        occurred_at=rows[0][2],
        question=common.get("question") or "",
        result=common.get("result") or "",
        legis_num=common.get("legis_num") or "",
        url=common.get("url") or "",
        votes=[],
    )
    for entity_id, event_type, _occ, payload in rows:
        p = json.loads(payload) if isinstance(payload, str) else payload
        bg = entity_id.split(":", 1)[1]
        ent = ent_mod.get(store, bg)
        if not ent:
            continue
        position = p.get("position") if event_type == "vote.cast" else "Not Voting"
        if not position:
            position = "Not Voting"
        party = p.get("party") or ""
        bucket = _ensure_party_bucket(detail.by_party, party)
        if position == "Yea":
            detail.yea += 1; bucket["Yea"] += 1
        elif position == "Nay":
            detail.nay += 1; bucket["Nay"] += 1
        elif position == "Present":
            detail.present += 1; bucket["Present"] += 1
        else:
            detail.missed += 1; bucket["Not Voting"] += 1
        detail.votes.append(VoteRecord(
            entity=ent,
            position=position,
            party=party,
            state=p.get("state") or ent.state,
            party_line=p.get("party_line"),
        ))
    return detail
