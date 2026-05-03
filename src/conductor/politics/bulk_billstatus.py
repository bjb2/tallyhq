"""Bulk BILLSTATUS loader from govinfo.gov.

Sidesteps api.congress.gov entirely. govinfo serves per-bill BILLSTATUS XML
that contains everything we need (sponsor, cosponsors, actions, text URLs)
in a single HTTP request — no key required, Cloudflare-cached, parallel-safe.

URL pattern:
  https://www.govinfo.gov/bulkdata/BILLSTATUS/{congress}/{btype}/BILLSTATUS-{congress}{btype}{num}.xml

Walks numbers per bill type sequentially via concurrent probe-batches; stops
when MAX_404_STREAK consecutive 404s after the last 200 in a batch.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable, Optional
from xml.etree import ElementTree as ET

import httpx

from conductor.events import Event, hash_payload
from conductor.politics import bills as bm
from conductor.store import Store

logger = logging.getLogger(__name__)

URL_TEMPLATE = (
    "https://www.govinfo.gov/bulkdata/BILLSTATUS/"
    "{congress}/{btype}/BILLSTATUS-{congress}{btype}{num}.xml"
)

ALL_BILL_TYPES = ("hr", "s", "hjres", "sjres", "hconres", "sconres", "hres", "sres")
MAX_404_STREAK = 6     # how many consecutive 404s before declaring "done"
PROBE_BATCH = 40       # numbers requested concurrently per probe


def _txt(el, path: str, default: str = "") -> str:
    found = el.find(path)
    if found is None or found.text is None:
        return default
    return found.text.strip()


def _date_or_none(s: Optional[str]):
    if not s:
        return None
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        return None


def _dt_or_now(s: Optional[str]) -> datetime:
    if not s:
        return datetime.now(tz=timezone.utc)
    s = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime.now(tz=timezone.utc)


@dataclass
class _Parsed:
    bill: bm.Bill
    events: list[Event]


def _parse_xml(xml_text: str) -> Optional[_Parsed]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    bill_el = root.find("bill")
    if bill_el is None:
        return None

    congress = _txt(bill_el, "congress")
    btype = _txt(bill_el, "type").lower()
    number = _txt(bill_el, "number")
    if not (congress and btype and number):
        return None
    try:
        number_int = int(number)
    except ValueError:
        return None
    bill_id = f"{congress}:{btype}:{number_int}"

    title = _txt(bill_el, "title")
    introduced = _date_or_none(_txt(bill_el, "introducedDate"))
    update_dt = _dt_or_now(_txt(bill_el, "updateDate"))

    latest_action_el = bill_el.find("latestAction")
    latest_action_text = _txt(latest_action_el, "text") if latest_action_el is not None else ""
    latest_action_date = _date_or_none(_txt(latest_action_el, "actionDate")) if latest_action_el is not None else None

    policy_area = _txt(bill_el.find("policyArea") or ET.Element("x"), "name")

    # Sponsors
    sponsor_bg = None
    sponsor_events: list[Event] = []
    sponsors_el = bill_el.find("sponsors")
    if sponsors_el is not None:
        first = sponsors_el.find("item")
        if first is not None:
            sponsor_bg = _txt(first, "bioguideId")

    bill_url = (
        f"https://www.congress.gov/bill/{congress}th-congress/"
        f"{_long_type(btype)}/{number_int}"
    )

    text_versions: list[dict] = []
    tv_el = bill_el.find("textVersions")
    if tv_el is not None:
        for tv in tv_el.findall("item"):
            formats: list[dict] = []
            for f in (tv.find("formats") or []):
                formats.append({"type": _txt(f, "type"), "url": _txt(f, "url")})
            text_versions.append({
                "type": _txt(tv, "type"),
                "date": _txt(tv, "date"),
                "formats": formats,
            })

    cosponsors_el = bill_el.find("cosponsors")
    cosponsor_count = 0
    if cosponsors_el is not None:
        items = cosponsors_el.findall("item")
        cosponsor_count = len(items)

    bill_obj = bm.Bill(
        bill_id=bill_id,
        congress=int(congress),
        bill_type=btype,
        number=number_int,
        title=title,
        sponsor_bioguide=sponsor_bg,
        introduced_date=introduced,
        latest_action_date=latest_action_date,
        latest_action_text=latest_action_text,
        policy_area=policy_area,
        url=bill_url,
        text_versions=text_versions,
        cosponsor_count=cosponsor_count,
    )

    events: list[Event] = []

    # bill.sponsored event for the primary sponsor
    if sponsor_bg:
        sp_payload = {
            "bill_id": bill_id,
            "congress": str(congress),
            "bill_type": btype,
            "number": number_int,
            "title": title,
            "introduced_date": introduced.isoformat() if introduced else None,
            "latest_action": latest_action_text,
            "url": bill_url,
        }
        sp_occ = datetime.combine(introduced, datetime.min.time(), tzinfo=timezone.utc) \
                 if introduced else update_dt
        events.append(Event(
            source="govinfo_billstatus",
            source_id=f"sponsor:{bill_id}:{sponsor_bg}",
            entity_id=f"bioguide:{sponsor_bg}",
            event_type="bill.sponsored",
            occurred_at=sp_occ,
            payload_hash=hash_payload(sp_payload),
            payload=sp_payload,
            schema_version=1,
        ))

    # bill.cosponsored events
    if cosponsors_el is not None:
        for item in cosponsors_el.findall("item"):
            bg = _txt(item, "bioguideId")
            if not bg:
                continue
            sd = _txt(item, "sponsorshipDate")
            sp_date = _date_or_none(sd)
            occ = (
                datetime.combine(sp_date, datetime.min.time(), tzinfo=timezone.utc)
                if sp_date else update_dt
            )
            payload = {
                "bill_id": bill_id,
                "congress": str(congress),
                "bill_type": btype,
                "number": number_int,
                "title": title,
                "is_original": _txt(item, "isOriginalCosponsor", "False") == "True",
                "url": bill_url,
            }
            events.append(Event(
                source="govinfo_billstatus",
                source_id=f"cosponsor:{bill_id}:{bg}",
                entity_id=f"bioguide:{bg}",
                event_type="bill.cosponsored",
                occurred_at=occ,
                payload_hash=hash_payload(payload),
                payload=payload,
                schema_version=1,
            ))

    # bill.action events (entity = bill)
    actions_el = bill_el.find("actions")
    if actions_el is not None:
        for a in actions_el.findall("item"):
            action_date = _date_or_none(_txt(a, "actionDate"))
            occ = (
                datetime.combine(action_date, datetime.min.time(), tzinfo=timezone.utc)
                if action_date else update_dt
            )
            action_code = _txt(a, "actionCode")
            action_type = _txt(a, "type")
            text = _txt(a, "text")
            committees: list[str] = []
            ce = a.find("committees")
            if ce is not None:
                committees = [_txt(c, "name") for c in ce.findall("item") if _txt(c, "name")]
            payload = {
                "bill_id": bill_id,
                "congress": str(congress),
                "bill_type": btype,
                "number": number_int,
                "action_date": action_date.isoformat() if action_date else None,
                "action_code": action_code,
                "action_type": action_type,
                "text": text,
                "committees": committees,
                "url": f"{bill_url}/all-actions",
            }
            events.append(Event(
                source="govinfo_billstatus",
                source_id=f"action:{bill_id}:{_txt(a, 'actionDate')}:{action_code or action_type}:{text[:48]}",
                entity_id=f"bill:{bill_id}",
                event_type="bill.action",
                occurred_at=occ,
                payload_hash=hash_payload(payload),
                payload=payload,
                schema_version=1,
            ))

    return _Parsed(bill=bill_obj, events=events)


def _long_type(t: str) -> str:
    return {
        "hr": "house-bill",
        "s": "senate-bill",
        "hjres": "house-joint-resolution",
        "sjres": "senate-joint-resolution",
        "hconres": "house-concurrent-resolution",
        "sconres": "senate-concurrent-resolution",
        "hres": "house-resolution",
        "sres": "senate-resolution",
    }.get(t, t)


async def _fetch_one(client: httpx.AsyncClient, congress: int, btype: str, num: int) -> tuple[int, Optional[str]]:
    url = URL_TEMPLATE.format(congress=congress, btype=btype, num=num)
    try:
        r = await client.get(url, timeout=20.0)
    except httpx.HTTPError:
        return -1, None
    if r.status_code == 404:
        return 404, None
    if r.status_code >= 400:
        return r.status_code, None
    return 200, r.text


async def _walk_type(
    store: Store,
    congress: int,
    btype: str,
    *,
    start: int = 1,
    end: Optional[int] = None,
    concurrency: int = 20,
    client: httpx.AsyncClient | None = None,
) -> dict[str, int]:
    """Walk one (congress, btype) range. Returns counters."""
    counts = {"fetched": 0, "bills_upserted": 0, "events_inserted": 0, "missing": 0}
    cursor_key = f"govinfo_billstatus:{congress}:{btype}"
    cur = store.get_cursor(cursor_key)
    last = start - 1
    if cur:
        try:
            last = max(last, int(cur))
        except ValueError:
            pass

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=20.0, follow_redirects=True)
    try:
        n = last
        streak_404 = 0
        while True:
            if end is not None and n >= end:
                break
            batch_nums = list(range(n + 1, n + 1 + PROBE_BATCH))
            results = await asyncio.gather(*[
                _fetch_one(client, congress, btype, k) for k in batch_nums
            ])
            highest_200 = None
            for k, (status, text) in zip(batch_nums, results):
                if status == 200 and text:
                    parsed = _parse_xml(text)
                    if parsed:
                        bm.upsert(store, parsed.bill)
                        counts["bills_upserted"] += 1
                        if parsed.events:
                            inserted = store.insert_events(parsed.events)
                            counts["events_inserted"] += inserted
                    counts["fetched"] += 1
                    streak_404 = 0
                    highest_200 = k
                elif status == 404:
                    streak_404 += 1
                    counts["missing"] += 1
                else:
                    streak_404 += 1
                    counts["missing"] += 1
            if highest_200 is not None:
                n = highest_200
                store.set_cursor(cursor_key, str(n))
            else:
                n += PROBE_BATCH
            if streak_404 >= MAX_404_STREAK + PROBE_BATCH:
                break
    finally:
        if own_client:
            await client.aclose()

    logger.info("[bulk %s/%s] %s", congress, btype, counts)
    return counts


async def bulk_load(
    store: Store,
    congress: int,
    bill_types: Iterable[str] = ALL_BILL_TYPES,
    *,
    concurrency: int = 20,
) -> dict[str, dict[str, int]]:
    bm.ensure_schema(store)
    results: dict[str, dict[str, int]] = {}
    async with httpx.AsyncClient(
        timeout=20.0,
        follow_redirects=True,
        limits=httpx.Limits(max_connections=concurrency * 2, max_keepalive_connections=concurrency),
    ) as client:
        for btype in bill_types:
            results[btype] = await _walk_type(
                store, congress, btype,
                concurrency=concurrency, client=client,
            )
    return results
