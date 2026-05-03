"""House of Representatives roll-call vote adapter.

Pulls per-roll-call XML from clerk.house.gov, fans out to one Event per
member position. Each vote.cast event carries `party_line` (member voted with
their party majority) so the activity-grid can reweight breaks.

URL pattern: https://clerk.house.gov/evs/{year}/roll{NNN}.xml
Cursor: "{year}:{last_roll}". Walks sequential roll numbers until 404.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime, timezone
from typing import AsyncIterator
from xml.etree import ElementTree as ET
from zoneinfo import ZoneInfo

import httpx

from conductor.adapters.base import Adapter, registry
from conductor.events import Event

logger = logging.getLogger(__name__)

URL_TEMPLATE = "https://clerk.house.gov/evs/{year}/roll{num:03}.xml"
EASTERN = ZoneInfo("America/New_York")

# Cap how many sequential 404s we tolerate before giving up the walk
MAX_404_STREAK = 3
# Cap rolls per pull so a fresh cold-start doesn't hammer clerk for hours
MAX_PER_PULL = 80


def _parse_action_dt(date_s: str | None, time_s: str | None) -> datetime:
    if not date_s:
        return datetime.now(tz=timezone.utc)
    try:
        d = datetime.strptime(date_s, "%d-%b-%Y").date()
    except ValueError:
        return datetime.now(tz=timezone.utc)
    hh, mm = 12, 0
    if time_s:
        try:
            hh, mm = (int(x) for x in time_s.strip().split(":")[:2])
        except ValueError:
            pass
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=EASTERN).astimezone(timezone.utc)


def _text(el, path: str, default: str = "") -> str:
    found = el.find(path)
    if found is None or found.text is None:
        return default
    return found.text.strip()


def _party_majorities(records: list[dict]) -> dict[str, str]:
    """For each party, the modal vote position (Yea/Nay/Present/Not Voting)."""
    by_party: dict[str, Counter] = {}
    for r in records:
        if r["vote"] in ("Yea", "Aye", "Nay", "No"):
            by_party.setdefault(r["party"], Counter())[r["vote"]] += 1
    out: dict[str, str] = {}
    for party, c in by_party.items():
        if c:
            out[party] = c.most_common(1)[0][0]
    # Normalize Aye→Yea, No→Nay
    return {p: ("Yea" if v in ("Yea", "Aye") else "Nay" if v in ("Nay", "No") else v) for p, v in out.items()}


def _normalize_vote(v: str) -> str:
    if v in ("Yea", "Aye"):
        return "Yea"
    if v in ("Nay", "No"):
        return "Nay"
    return v  # Present, Not Voting


@registry.register
class CongressRollcallsAdapter(Adapter):
    name = "congress_rollcalls"
    schema_version = 1

    async def _fetch(self, year: int, num: int) -> str | None:
        url = URL_TEMPLATE.format(year=year, num=num)
        try:
            status, text = await self.http.get_text(url)
        except httpx.HTTPError as e:
            logger.warning("HTTP error %s: %s", url, e)
            return None
        if status == 404:
            return None
        return text

    def _parse(self, year: int, num: int, xml_text: str) -> list[Event]:
        root = ET.fromstring(xml_text)
        meta = root.find("vote-metadata")
        if meta is None:
            return []

        congress = _text(meta, "congress")
        session = _text(meta, "session")
        legis_num = _text(meta, "legis-num")
        question = _text(meta, "vote-question")
        vote_type = _text(meta, "vote-type")
        result = _text(meta, "vote-result")
        action_date = _text(meta, "action-date")
        action_time_el = meta.find("action-time")
        action_time = (
            (action_time_el.attrib.get("time-etz") or (action_time_el.text or ""))
            if action_time_el is not None
            else ""
        )
        occurred_at = _parse_action_dt(action_date, action_time)

        # First pass — collect raw records for party-majority computation
        records: list[dict] = []
        vd = root.find("vote-data")
        if vd is None:
            return []
        for rv in vd.findall("recorded-vote"):
            leg = rv.find("legislator")
            vote_el = rv.find("vote")
            if leg is None or vote_el is None or vote_el.text is None:
                continue
            name_id = leg.attrib.get("name-id") or ""
            if not name_id:
                continue
            records.append({
                "name_id": name_id,
                "name": (leg.text or "").strip(),
                "party": leg.attrib.get("party") or "",
                "state": leg.attrib.get("state") or "",
                "vote": _normalize_vote(vote_el.text.strip()),
            })

        majorities = _party_majorities(records)
        events: list[Event] = []
        roll_id = f"H:{year}:{num}"
        common_payload = {
            "chamber": "house",
            "year": year,
            "rollcall_num": num,
            "congress": congress,
            "session": session,
            "legis_num": legis_num,
            "question": question,
            "vote_type": vote_type,
            "result": result,
            "url": URL_TEMPLATE.format(year=year, num=num),
        }

        for r in records:
            entity_id = f"bioguide:{r['name_id']}"
            position = r["vote"]
            party_majority = majorities.get(r["party"])
            party_line: bool | None
            if position in ("Not Voting", "Present"):
                event_type = "vote.missed" if position == "Not Voting" else "vote.cast"
                party_line = None
            else:
                event_type = "vote.cast"
                party_line = (party_majority is not None and position == party_majority)

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
                    source_id=f"{roll_id}:{r['name_id']}",
                    entity_id=entity_id,
                    event_type=event_type,
                    payload=payload,
                    occurred_at=occurred_at,
                    schema_version=self.schema_version,
                )
            )

        return events

    def _cursor(self) -> tuple[int, int]:
        raw = self.store.get_cursor(self.name)
        if raw and ":" in raw:
            y, n = raw.split(":", 1)
            try:
                return int(y), int(n)
            except ValueError:
                pass
        # Cold start: current year, roll 0 (so we walk from 1)
        return date.today().year, 0

    def _set_cursor(self, year: int, num: int) -> None:
        self.store.set_cursor(self.name, f"{year}:{num}")

    async def pull(self) -> AsyncIterator[Event]:
        year, last = self._cursor()
        current_year = date.today().year
        pulled = 0
        streak_404 = 0

        # Walk current cursor year
        n = last
        while pulled < MAX_PER_PULL and streak_404 < MAX_404_STREAK:
            n += 1
            xml_text = await self._fetch(year, n)
            if xml_text is None:
                streak_404 += 1
                continue
            streak_404 = 0
            for ev in self._parse(year, n, xml_text):
                yield ev
            self._set_cursor(year, n)
            pulled += 1

        # If we hit end-of-year wall and a newer year exists, jump
        if streak_404 >= MAX_404_STREAK and year < current_year:
            year = year + 1
            n = 0
            self._set_cursor(year, n)
            streak_404 = 0
            while pulled < MAX_PER_PULL and streak_404 < MAX_404_STREAK:
                n += 1
                xml_text = await self._fetch(year, n)
                if xml_text is None:
                    streak_404 += 1
                    continue
                streak_404 = 0
                for ev in self._parse(year, n, xml_text):
                    yield ev
                self._set_cursor(year, n)
                pulled += 1
