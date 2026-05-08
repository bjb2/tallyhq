"""Amendments adapter — api.congress.gov /amendment.

Each amendment carries a sponsor (member) + parent bill linkage. We emit one
`floor.amendment_offered` event per amendment for the sponsor's entity.

Cursor: ISO datetime of max updateDate seen. Same shape as congress_bills.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

import httpx

from conductor.adapters.base import Adapter, registry
from conductor.events import Event
from conductor.secrets import require

logger = logging.getLogger(__name__)

API_BASE = "https://api.congress.gov/v3"
LIST_LIMIT = 250
MAX_PAGES = 8
MAX_AMENDMENTS_PER_PULL = 200


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
class CongressAmendmentsAdapter(Adapter):
    name = "congress_amendments"
    schema_version = 1

    @property
    def api_key(self) -> str:
        return require("CONGRESS_GOV_API_KEY")

    async def _get_json(self, path: str, params: dict | None = None) -> dict:
        params = dict(params or {})
        params["format"] = "json"
        url = f"{API_BASE}{path}"
        client = self.http._client
        if client is None:
            raise RuntimeError("HttpClient not entered")
        r = await client.get(url, params=params, headers={"X-Api-Key": self.api_key})
        if r.status_code >= 400:
            r.raise_for_status()
        return r.json()

    def _cursor(self) -> datetime | None:
        raw = self.store.get_cursor(self.name)
        return _parse_dt(raw) if raw else None

    def _set_cursor(self, dt: datetime) -> None:
        self.store.set_cursor(self.name, dt.isoformat())

    async def pull(self) -> AsyncIterator[Event]:
        cursor = self._cursor()
        if cursor is None:
            cursor = datetime.now(tz=timezone.utc) - timedelta(days=30)
            logger.info("cold start — pulling amendments since %s", cursor.isoformat())
        else:
            cursor = cursor - timedelta(days=4)
            logger.info("applying 4-day lookback overlap — pulling since %s", cursor.isoformat())

        offset = 0
        amendments: list[dict] = []
        for _ in range(MAX_PAGES):
            data = await self._get_json(
                "/amendment",
                params={
                    "limit": LIST_LIMIT,
                    "offset": offset,
                    "fromDateTime": cursor.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "sort": "updateDate+asc",
                },
            )
            page = data.get("amendments") or []
            if not page:
                break
            amendments.extend(page)
            offset += LIST_LIMIT
            if len(page) < LIST_LIMIT:
                break

        logger.info("[%s] %d amendments updated since cursor (processing up to %d)",
                    self.name, len(amendments), MAX_AMENDMENTS_PER_PULL)
        amendments = amendments[:MAX_AMENDMENTS_PER_PULL]

        max_seen = cursor
        for a in amendments:
            try:
                async for ev in self._process_amendment(a):
                    yield ev
                    if ev.occurred_at > max_seen:
                        max_seen = ev.occurred_at
            except httpx.HTTPError as e:
                logger.warning("skip amendment on HTTP error: %s", e)
                continue
            await asyncio.sleep(0.04)

        if max_seen > cursor:
            self._set_cursor(max_seen)

    async def _process_amendment(self, a: dict) -> AsyncIterator[Event]:
        congress = a.get("congress")
        amdt_type = (a.get("type") or "").lower()  # samdt | hamdt | suamdt
        number = a.get("number")
        if not (congress and amdt_type and number):
            return

        # Detail fetch — sponsors and parent-bill linkage live there
        detail = await self._get_json(f"/amendment/{congress}/{amdt_type}/{number}")
        amend = detail.get("amendment") or {}

        sponsors = amend.get("sponsors") or []
        if not sponsors:
            return
        sp = sponsors[0]
        bioguide = sp.get("bioguideId")
        if not bioguide:
            return

        submitted = _parse_dt(a.get("updateDate"))
        if amend.get("submittedDate"):
            submitted = _parse_dt(amend["submittedDate"])

        amended_bill = amend.get("amendedBill") or {}
        bill_id = None
        if amended_bill.get("congress") and amended_bill.get("type") and amended_bill.get("number"):
            bill_id = f"{amended_bill['congress']}:{amended_bill['type'].lower()}:{amended_bill['number']}"

        chamber = "house" if amdt_type.startswith("h") else "senate"
        amdt_id = f"{congress}:{amdt_type}:{number}"
        purpose = amend.get("purpose") or amend.get("description") or ""
        latest_action = ((amend.get("latestAction") or {}).get("text") or "")

        payload = {
            "amendment_id": amdt_id,
            "congress": str(congress),
            "amendment_type": amdt_type,
            "number": number,
            "chamber": chamber,
            "purpose": purpose,
            "latest_action": latest_action,
            "bill_id": bill_id,
            "url": f"https://www.congress.gov/amendment/{congress}th-congress/"
                   f"{'house' if chamber == 'house' else 'senate'}-amendment/{number}",
        }
        yield Event.build(
            source=self.name,
            source_id=f"amend:{amdt_id}:{bioguide}",
            entity_id=f"bioguide:{bioguide}",
            event_type="floor.amendment_offered",
            payload=payload,
            occurred_at=submitted,
            schema_version=self.schema_version,
        )
