"""api.congress.gov bills + cosponsors adapter.

Discovers bills updated since cursor, emits:
  - bill.sponsored      (one per bill, for the primary sponsor)
  - bill.cosponsored    (one per cosponsor, for each bill)

Cursor format: ISO datetime of max updateDate seen.

Rate limit: 5,000 requests/hour with key.
Pagination: limit=250 (max). We page until updateDate < cursor.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import AsyncIterator

import httpx

from conductor.adapters.base import Adapter, registry
from conductor.events import Event
from conductor.politics import bills as bills_mod
from conductor.secrets import require

logger = logging.getLogger(__name__)

API_BASE = "https://api.congress.gov/v3"
LIST_LIMIT = 250  # max per request
MAX_PAGES = 20    # cap total bills per pull (~5000 bills)
MAX_BILLS_PER_PULL = 200  # detail+cosp fetches are expensive — bound them per pull
COSPONSOR_LIMIT = 250


def _parse_dt(s: str | None) -> datetime:
    if not s:
        return datetime.now(tz=timezone.utc)
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return datetime.now(tz=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@registry.register
class CongressBillsAdapter(Adapter):
    name = "congress_bills"
    schema_version = 1

    @property
    def api_key(self) -> str:
        return require("CONGRESS_GOV_API_KEY")

    async def _get_json(self, path: str, params: dict | None = None) -> dict:
        # Use header auth so api_key never appears in URL logs.
        params = dict(params or {})
        params["format"] = "json"
        url = f"{API_BASE}{path}"
        client = self.http._client
        if client is None:
            raise RuntimeError("HttpClient not entered")
        try:
            r = await client.get(url, params=params, headers={"X-Api-Key": self.api_key})
            if r.status_code >= 400:
                r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as e:
            logger.warning("api.congress.gov %s -> %s", path, e)
            raise

    def _cursor(self) -> datetime | None:
        raw = self.store.get_cursor(self.name)
        if not raw:
            return None
        return _parse_dt(raw)

    def _set_cursor(self, dt: datetime) -> None:
        self.store.set_cursor(self.name, dt.isoformat())

    async def pull(self) -> AsyncIterator[Event]:
        # Eagerly create entity schema so an early failure doesn't leave
        # partial state.
        bills_mod.ensure_schema(self.store)

        cursor = self._cursor()
        # Cold start: pull last 30 days. Otherwise pull from cursor.
        if cursor is None:
            from datetime import timedelta
            cursor = datetime.now(tz=timezone.utc) - timedelta(days=30)
            logger.info("cold start — pulling bills updated since %s", cursor.isoformat())

        offset = 0
        max_seen: datetime = cursor
        bills: list[dict] = []

        for page in range(MAX_PAGES):
            data = await self._get_json(
                "/bill",
                params={
                    "limit": LIST_LIMIT,
                    "offset": offset,
                    "fromDateTime": cursor.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "sort": "updateDate+asc",
                },
            )
            page_bills = data.get("bills") or []
            if not page_bills:
                break
            bills.extend(page_bills)
            offset += LIST_LIMIT
            # If page returned fewer than limit, end of results
            if len(page_bills) < LIST_LIMIT:
                break

        logger.info("[%s] %d bills updated since cursor (processing up to %d)",
                    self.name, len(bills), MAX_BILLS_PER_PULL)
        bills = bills[:MAX_BILLS_PER_PULL]

        for b in bills:
            try:
                async for ev in self._process_bill(b):
                    yield ev
            except httpx.HTTPError as e:
                logger.warning("skipping bill on HTTP error: %s", e)
                continue
            # Advance cursor by the BILL's updateDate (sort order is updateDate+asc),
            # not by event occurred_at — sponsor/cosp events carry introduced_date
            # which can be much older than the cursor and would freeze it.
            upd = _parse_dt(b.get("updateDate"))
            if upd > max_seen:
                max_seen = upd

        if max_seen > cursor:
            self._set_cursor(max_seen)

    async def _process_bill(self, b: dict) -> AsyncIterator[Event]:
        congress = b.get("congress")
        bill_type = (b.get("type") or "").lower()
        number = b.get("number")
        if not (congress and bill_type and number):
            return

        update_dt = _parse_dt(b.get("updateDate"))
        title = b.get("title") or ""
        bill_id = f"{congress}:{bill_type}:{number}"
        bill_entity = f"bill:{bill_id}"

        # Detail fetch — needed for sponsor bioguideId + introducedDate
        detail = await self._get_json(f"/bill/{congress}/{bill_type}/{number}")
        bill = detail.get("bill") or {}
        sponsors = bill.get("sponsors") or []
        introduced_str = bill.get("introducedDate")
        introduced_date_only = None
        if introduced_str:
            try:
                from datetime import date as _date
                introduced_date_only = _date.fromisoformat(introduced_str)
            except ValueError:
                pass
        introduced = _parse_dt((introduced_str + "T12:00:00Z") if introduced_str else None)
        latest_action_obj = bill.get("latestAction") or {}
        latest_action = latest_action_obj.get("text") or ""
        latest_action_date = None
        if latest_action_obj.get("actionDate"):
            try:
                from datetime import date as _date
                latest_action_date = _date.fromisoformat(latest_action_obj["actionDate"])
            except ValueError:
                pass

        # Upsert into bills entity table
        sponsor_bg = sponsors[0].get("bioguideId") if sponsors else None
        bill_url = (
            f"https://www.congress.gov/bill/{congress}th-congress/"
            f"{_long_type(bill_type)}/{number}"
        )
        b_entity = bills_mod.Bill(
            bill_id=bill_id,
            congress=int(congress),
            bill_type=bill_type,
            number=int(number),
            title=title,
            sponsor_bioguide=sponsor_bg,
            introduced_date=introduced_date_only,
            latest_action_date=latest_action_date,
            latest_action_text=latest_action,
            policy_area=(bill.get("policyArea") or {}).get("name") or "",
            url=bill_url,
            text_versions=[],
            cosponsor_count=int((bill.get("cosponsors") or {}).get("count") or 0),
        )
        bills_mod.upsert(self.store, b_entity)

        # Sponsor event
        if sponsors:
            sp = sponsors[0]
            bioguide = sp.get("bioguideId")
            if bioguide:
                payload = {
                    "bill_id": bill_id,
                    "congress": congress,
                    "bill_type": bill_type,
                    "number": number,
                    "title": title,
                    "introduced_date": bill.get("introducedDate"),
                    "latest_action": latest_action,
                    "url": f"https://www.congress.gov/bill/{congress}th-congress/{_long_type(bill_type)}/{number}",
                }
                yield Event.build(
                    source=self.name,
                    source_id=f"sponsor:{bill_id}:{bioguide}",
                    entity_id=f"bioguide:{bioguide}",
                    event_type="bill.sponsored",
                    payload=payload,
                    occurred_at=introduced,
                    schema_version=self.schema_version,
                )

        # Cosponsors fetch (paginated)
        cosp_offset = 0
        while True:
            cdata = await self._get_json(
                f"/bill/{congress}/{bill_type}/{number}/cosponsors",
                params={"limit": COSPONSOR_LIMIT, "offset": cosp_offset},
            )
            cosp_list = cdata.get("cosponsors") or []
            if not cosp_list:
                break
            for c in cosp_list:
                bioguide = c.get("bioguideId")
                if not bioguide:
                    continue
                co_dt = _parse_dt(
                    (c.get("sponsorshipDate") + "T12:00:00Z") if c.get("sponsorshipDate") else None
                )
                payload = {
                    "bill_id": bill_id,
                    "congress": congress,
                    "bill_type": bill_type,
                    "number": number,
                    "title": title,
                    "is_original": c.get("isOriginalCosponsor"),
                    "url": f"https://www.congress.gov/bill/{congress}th-congress/{_long_type(bill_type)}/{number}",
                }
                yield Event.build(
                    source=self.name,
                    source_id=f"cosponsor:{bill_id}:{bioguide}",
                    entity_id=f"bioguide:{bioguide}",
                    event_type="bill.cosponsored",
                    payload=payload,
                    occurred_at=co_dt,
                    schema_version=self.schema_version,
                )
            cosp_offset += COSPONSOR_LIMIT
            if len(cosp_list) < COSPONSOR_LIMIT:
                break

        # Tiny politeness sleep — avoid bursting full rate limit on cold start
        await asyncio.sleep(0.05)


def _long_type(t: str) -> str:
    """Convert short bill type (hr, s, hjres, sjres, hconres, sconres, hres, sres)
    to congress.gov URL slug."""
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
