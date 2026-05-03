"""GovInfo Congressional Record (CREC) floor-speech adapter.

Metadata-only ingest from per-day MODS XML on govinfo.gov bulk data.
Emits one `floor.speech` event per (granule, speaker bioguide). Multi-speaker
colloquies fan out to N events sharing the same accessId.

URL pattern: https://www.govinfo.gov/metadata/pkg/CREC-{YYYY-MM-DD}/mods.xml
Cursor: ISO date YYYY-MM-DD of last successfully processed day.
Cold-start: 119th Congress began 2025-01-03.

Skips:
  - granuleClass == DAILYDIGEST (summary, not speech)
  - granules with no <congMember bioGuideId="...">
  - 404 days (weekends/holidays/no-session)
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, timedelta, timezone
from typing import AsyncIterator
from xml.etree import ElementTree as ET

import httpx

from conductor.adapters.base import Adapter, registry
from conductor.events import Event

logger = logging.getLogger(__name__)

MODS_URL = "https://www.govinfo.gov/metadata/pkg/CREC-{date}/mods.xml"
NS = {"mods": "http://www.loc.gov/mods/v3"}
M = "{http://www.loc.gov/mods/v3}"

CONGRESS_119_START = date(2025, 1, 3)
DEFAULT_DAYS_PER_PULL = int(os.environ.get("CREC_DAYS_PER_PULL", "7"))
DEFAULT_CONCURRENCY = int(os.environ.get("CREC_CONCURRENCY", "8"))


def _granule_chamber(granule_class: str) -> str:
    gc = (granule_class or "").upper()
    if gc == "HOUSE":
        return "house"
    if gc == "SENATE":
        return "senate"
    if gc == "EXTENSIONS":
        return "extensions"
    return gc.lower()


def _parse_mods(day: date, xml_text: str) -> tuple[list[Event], dict]:
    """Parse one day's MODS into floor.speech events.

    Returns (events, stats) where stats reports skipped granules.
    """
    stats = {"granules": 0, "skipped_digest": 0, "skipped_no_member": 0, "events": 0}
    if not xml_text:
        return [], stats
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        # Empty body / HTML redirect to /error — treat as no-session day
        logger.debug("MODS parse error for %s (likely no-session): %s", day, e)
        return [], stats

    occurred_at = datetime(day.year, day.month, day.day, 12, 0, tzinfo=timezone.utc)
    events: list[Event] = []

    for it in root.findall("mods:relatedItem[@type='constituent']", NS):
        stats["granules"] += 1

        ext = it.find("mods:extension", NS)
        granule_class = ""
        access_id = ""
        bill_refs: list[dict] = []
        cong_members = []
        if ext is not None:
            gc_el = ext.find("mods:granuleClass", NS)
            granule_class = (gc_el.text or "").strip() if gc_el is not None else ""
            aid_el = ext.find("mods:accessId", NS)
            access_id = (aid_el.text or "").strip() if aid_el is not None else ""
            cong_members = ext.findall("mods:congMember", NS)
            for irt in ext.findall("mods:isReferringTo", NS):
                attrs = dict(irt.attrib)
                # Capture bill-shaped refs only
                if attrs.get("type") in ("bill", "law", "publiclaw") or "bill" in attrs.get("ID", "").lower():
                    bill_refs.append({
                        "type": attrs.get("type"),
                        "ref": attrs.get("ID") or attrs.get("xlink:href") or (irt.text or "").strip()[:80],
                    })

        if granule_class.upper() == "DAILYDIGEST":
            stats["skipped_digest"] += 1
            continue
        if not cong_members:
            stats["skipped_no_member"] += 1
            continue
        if not access_id:
            continue

        # Title
        title = ""
        ti = it.find("mods:titleInfo/mods:title", NS)
        if ti is not None and ti.text:
            title = ti.text.strip()

        # Citation
        citation = ""
        for ident in it.findall("mods:identifier", NS):
            if ident.attrib.get("type") == "preferred citation" and ident.text:
                citation = ident.text.strip()
                break

        # Page range (first <part> with extent)
        page_start = page_end = None
        for p in it.findall("mods:part", NS):
            ex = p.find("mods:extent", NS)
            if ex is None:
                continue
            s = ex.find("mods:start", NS)
            e = ex.find("mods:end", NS)
            page_start = (s.text or "").strip() if s is not None and s.text else None
            page_end = (e.text or "").strip() if e is not None and e.text else None
            if page_start or page_end:
                break

        chamber = _granule_chamber(granule_class)
        speaker_count = len(cong_members)
        granule_url = f"https://www.govinfo.gov/app/details/CREC-{day.isoformat()}/{access_id}"

        for cm in cong_members:
            bioguide = (cm.attrib.get("bioGuideId") or "").strip()
            if not bioguide:
                continue
            congress_s = cm.attrib.get("congress") or ""
            try:
                congress_int = int(congress_s) if congress_s else None
            except ValueError:
                congress_int = None
            role = cm.attrib.get("role") or ""

            payload = {
                "chamber": chamber,
                "congress": congress_int,
                "title": title,
                "citation": citation,
                "page_start": page_start,
                "page_end": page_end,
                "granule_class": granule_class,
                "granule_url": granule_url,
                "role": role,
                "speaker_count": speaker_count,
                "party": cm.attrib.get("party") or "",
                "state": cm.attrib.get("state") or "",
                "date": day.isoformat(),
            }
            if bill_refs:
                payload["bill_refs"] = bill_refs

            events.append(
                Event.build(
                    source="govinfo_crec",
                    source_id=f"{access_id}:{bioguide}",
                    entity_id=f"bioguide:{bioguide}",
                    event_type="floor.speech",
                    payload=payload,
                    occurred_at=occurred_at,
                    schema_version=1,
                )
            )
            stats["events"] += 1

    return events, stats


@registry.register
class GovInfoCrecAdapter(Adapter):
    """Walks days from cursor forward; metadata-only floor.speech events."""

    name = "govinfo_crec"
    schema_version = 1

    async def _fetch_day(self, day: date) -> str | None:
        url = MODS_URL.format(date=day.isoformat())
        try:
            status, text = await self.http.get_text(url)
        except httpx.HTTPError as e:
            logger.warning("HTTP error %s: %s", url, e)
            return None
        if status == 404:
            return None
        # Empty body or HTML redirect (govinfo 302→/error for no-session days)
        # Empty body or HTML redirect (govinfo 302→/error for no-session days)
        stripped = text.lstrip() if text else ""
        if not stripped:
            return None
        if not (stripped.startswith("<?xml") or stripped.startswith("<mods")):
            return None
        return text

    def _cursor_date(self) -> date:
        raw = self.store.get_cursor(self.name)
        if raw:
            try:
                return date.fromisoformat(raw)
            except ValueError:
                pass
        # Cold-start: day before 119th opening so the first walk includes Jan 3
        return CONGRESS_119_START - timedelta(days=1)

    def _set_cursor(self, day: date) -> None:
        self.store.set_cursor(self.name, day.isoformat())

    async def pull(
        self,
        *,
        days: int | None = None,
        end_date: date | None = None,
        concurrency: int | None = None,
    ) -> AsyncIterator[Event]:
        days = days or DEFAULT_DAYS_PER_PULL
        concurrency = concurrency or DEFAULT_CONCURRENCY
        last = self._cursor_date()
        today = end_date or date.today()
        # Days to walk: cursor+1 .. min(cursor+days, today)
        first = last + timedelta(days=1)
        if first > today:
            return
        last_walk = min(first + timedelta(days=days - 1), today)
        walk = []
        d = first
        while d <= last_walk:
            walk.append(d)
            d += timedelta(days=1)

        sem = asyncio.Semaphore(concurrency)

        async def _fetch(day: date) -> tuple[date, str | None]:
            async with sem:
                return day, await self._fetch_day(day)

        # Fetch in parallel but yield/advance cursor in chronological order
        results = await asyncio.gather(*(_fetch(d) for d in walk))
        results_by_day = {d: t for d, t in results}

        for day in walk:
            xml_text = results_by_day.get(day)
            if xml_text is None:
                # 404 / no session — advance cursor and continue
                self._set_cursor(day)
                continue
            events, stats = _parse_mods(day, xml_text)
            logger.info(
                "[govinfo_crec] %s granules=%d events=%d skip_digest=%d skip_no_member=%d",
                day, stats["granules"], stats["events"],
                stats["skipped_digest"], stats["skipped_no_member"],
            )
            for ev in events:
                yield ev
            self._set_cursor(day)
