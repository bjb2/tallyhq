"""Senate roll-call vote adapter.

Pulls per-vote XML from senate.gov, fans out to one Event per senator position.
Senate XML uses LIS member IDs, not bioguide. We resolve via the legislators
crosswalk (ids.lis).

URL pattern: senate.gov/legislative/LIS/roll_call_votes/vote{C}{S}/vote_{C}_{S}_{NNNNN}.xml
Cursor: "{congress}:{session}:{last_vote_num}". Walks sequential vote numbers.

Senate sessions reset numbering each session (1 or 2). Most years cover one session.
"""
from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import date, datetime, timezone
from typing import AsyncIterator, Optional
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import httpx

from conductor.adapters.base import Adapter, registry
from conductor.events import Event

logger = logging.getLogger(__name__)

URL_TEMPLATE = (
    "https://www.senate.gov/legislative/LIS/roll_call_votes/"
    "vote{congress}{session}/vote_{congress}_{session}_{num:05}.xml"
)
EASTERN = ZoneInfo("America/New_York")

MAX_404_STREAK = 3
MAX_PER_PULL = 80


def _parse_vote_date(s: str) -> datetime:
    """Senate format: 'February 4, 2025, 03:30 PM'."""
    if not s:
        return datetime.now(tz=timezone.utc)
    s = s.strip().rstrip(".")
    for fmt in ("%B %d, %Y, %I:%M %p", "%B %d, %Y, %H:%M", "%B %d, %Y"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=EASTERN).astimezone(timezone.utc)
        except ValueError:
            continue
    return datetime.now(tz=timezone.utc)


def _text(el, path: str, default: str = "") -> str:
    found = el.find(path)
    if found is None or found.text is None:
        return default
    return found.text.strip()


def _normalize_vote(v: str) -> str:
    if v in ("Yea", "Aye"):
        return "Yea"
    if v in ("Nay", "No"):
        return "Nay"
    if v in ("Not Voting",):
        return "Not Voting"
    if v in ("Present",):
        return "Present"
    return v


def _party_majorities(records: list[dict]) -> dict[str, str]:
    by_party: dict[str, Counter] = {}
    for r in records:
        if r["vote"] in ("Yea", "Aye", "Nay", "No"):
            by_party.setdefault(r["party"], Counter())[r["vote"]] += 1
    out: dict[str, str] = {}
    for party, c in by_party.items():
        if c:
            out[party] = c.most_common(1)[0][0]
    return {p: ("Yea" if v in ("Yea", "Aye") else "Nay" if v in ("Nay", "No") else v) for p, v in out.items()}


@registry.register
class SenateRollcallsAdapter(Adapter):
    name = "senate_rollcalls"
    schema_version = 1

    def _build_lis_index(self) -> dict[str, str]:
        """LIS member-id → bioguide. Built from legislators table."""
        rows = self.store.conn.execute(
            "SELECT bioguide_id, ids FROM legislators WHERE chamber = 'senate'"
        ).fetchall()
        idx: dict[str, str] = {}
        for bg, ids_json in rows:
            try:
                ids = json.loads(ids_json) if ids_json else {}
            except (TypeError, ValueError):
                continue
            lis = ids.get("lis")
            if lis:
                idx[str(lis).strip()] = bg
        return idx

    def _build_lastname_index(self) -> dict[tuple[str, str], str]:
        """(last_name lowercase, state) → bioguide. Fallback for Senate
        votes whose XML lacks lis_member_id (older votes do)."""
        rows = self.store.conn.execute(
            "SELECT bioguide_id, last_name, state FROM legislators WHERE chamber = 'senate'"
        ).fetchall()
        return {(ln.lower(), st): bg for bg, ln, st in rows if ln}

    async def _fetch(self, congress: int, session: int, num: int) -> Optional[str]:
        url = URL_TEMPLATE.format(congress=congress, session=session, num=num)
        try:
            status, text = await self.http.get_text(url)
        except httpx.HTTPError as e:
            logger.warning("HTTP error %s: %s", url, e)
            return None
        if status == 404:
            return None
        return text

    def _parse(
        self,
        xml_text: str,
        congress: int,
        session: int,
        num: int,
        lis_idx: dict[str, str],
        ln_idx: dict[tuple[str, str], str],
    ) -> list[Event]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            logger.warning("XML parse fail vote %s_%s_%s: %s", congress, session, num, e)
            return []

        question = _text(root, "vote_question_text")
        result = _text(root, "vote_result")
        vote_date = _parse_vote_date(_text(root, "vote_date"))

        # Bill linkage — Senate XML <document> may identify the underlying bill
        doc = root.find("document")
        legis_num = ""
        if doc is not None:
            dt = _text(doc, "document_type")
            dn = _text(doc, "document_number")
            if dt and dn:
                # Build a House-clerk-style legis_num so parse_legis_num catches it
                # H.R. 1234, S. 12, S.J.RES. 5, etc.
                normalized_type = {
                    "S": "S",
                    "HR": "H R",
                    "H": "H R",
                    "SJRES": "S J RES",
                    "HJRES": "H J RES",
                    "SCONRES": "S CON RES",
                    "HCONRES": "H CON RES",
                    "SRES": "S RES",
                    "HRES": "H RES",
                }.get(dt.upper().replace(".", "").replace(" ", ""), dt)
                legis_num = f"{normalized_type} {dn}"

        # Walk member positions
        members_el = root.find("members")
        if members_el is None:
            return []

        records: list[dict] = []
        for m in members_el.findall("member"):
            lis_id = _text(m, "lis_member_id")
            last = _text(m, "last_name")
            state = _text(m, "state")
            party = _text(m, "party")
            cast = _text(m, "vote_cast")

            bioguide = lis_idx.get(lis_id) or ln_idx.get((last.lower(), state))
            if not bioguide:
                continue
            records.append({
                "bioguide": bioguide,
                "party": party,
                "state": state,
                "vote": _normalize_vote(cast),
            })

        majorities = _party_majorities(records)
        events: list[Event] = []
        roll_id = f"S:{congress}:{session}:{num}"
        common_payload = {
            "chamber": "senate",
            "congress": str(congress),
            "session": str(session),
            "rollcall_num": num,
            "question": question,
            "result": result,
            "legis_num": legis_num,
            "url": URL_TEMPLATE.format(congress=congress, session=session, num=num),
        }

        for r in records:
            position = r["vote"]
            party_majority = majorities.get(r["party"])
            party_line: Optional[bool]
            if position == "Not Voting":
                event_type = "vote.missed"
                party_line = None
            elif position in ("Yea", "Nay"):
                event_type = "vote.cast"
                party_line = (party_majority is not None and position == party_majority)
            else:  # Present, others
                event_type = "vote.cast"
                party_line = None

            payload = {
                **common_payload,
                "position": position,
                "party": r["party"],
                "state": r["state"],
                "party_line": party_line,
            }
            events.append(
                Event.build(
                    source=self.name,
                    source_id=f"{roll_id}:{r['bioguide']}",
                    entity_id=f"bioguide:{r['bioguide']}",
                    event_type=event_type,
                    payload=payload,
                    occurred_at=vote_date,
                    schema_version=self.schema_version,
                )
            )
        return events

    def _cursor(self) -> tuple[int, int, int]:
        """Returns (congress, session, last_vote_num). Defaults to (119, 1, 0)."""
        raw = self.store.get_cursor(self.name)
        if raw and raw.count(":") >= 2:
            try:
                c, s, n = raw.split(":")[:3]
                return int(c), int(s), int(n)
            except ValueError:
                pass
        # 119th Congress sessions: 1 = 2025, 2 = 2026
        today = date.today()
        congress = 119 if today.year >= 2025 else 118
        session = 2 if today.year % 2 == 0 else 1
        return congress, session, 0

    def _set_cursor(self, congress: int, session: int, num: int) -> None:
        self.store.set_cursor(self.name, f"{congress}:{session}:{num}")

    async def pull(self) -> AsyncIterator[Event]:
        lis_idx = self._build_lis_index()
        ln_idx = self._build_lastname_index()
        congress, session, last = self._cursor()
        pulled = 0
        streak_404 = 0
        n = last

        while pulled < MAX_PER_PULL and streak_404 < MAX_404_STREAK:
            n += 1
            xml_text = await self._fetch(congress, session, n)
            if xml_text is None:
                streak_404 += 1
                continue
            streak_404 = 0
            for ev in self._parse(xml_text, congress, session, n, lis_idx, ln_idx):
                yield ev
            self._set_cursor(congress, session, n)
            pulled += 1

        # Session boundary — try next session if we hit end-of-session wall
        if streak_404 >= MAX_404_STREAK:
            today = date.today()
            current_session = 2 if today.year % 2 == 0 else 1
            if session < current_session:
                session += 1
                n = 0
                self._set_cursor(congress, session, n)
                streak_404 = 0
                while pulled < MAX_PER_PULL and streak_404 < MAX_404_STREAK:
                    n += 1
                    xml_text = await self._fetch(congress, session, n)
                    if xml_text is None:
                        streak_404 += 1
                        continue
                    streak_404 = 0
                    for ev in self._parse(xml_text, congress, session, n, lis_idx, ln_idx):
                        yield ev
                    self._set_cursor(congress, session, n)
                    pulled += 1
