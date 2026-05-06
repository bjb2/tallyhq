"""Bill action timeline adapter — api.congress.gov /bill/{c}/{t}/{n}/actions.

Each action is a stage transition (introduced → committee → reported → floor →
passed → enrolled → signed). Stored as `bill.action` events keyed to the bill
entity (entity_id = "bill:{bill_id}"), since actions are facts about the bill,
not about a single member.

Re-pulled per bill on each round so we capture late-arriving actions. Cursor
tracks the bill list scan, not individual actions; dedupe lives in the event
store via (source, source_id, payload_hash).

Run after bills are populated: actions are joined to bills by bill_id, so this
adapter only fetches actions for bills already in the bills entity table.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import AsyncIterator

import httpx

from conductor.adapters.base import Adapter, registry
from conductor.events import Event
from conductor.secrets import require

logger = logging.getLogger(__name__)

API_BASE = "https://api.congress.gov/v3"
ACTIONS_LIMIT = 250

# Refresh actions only for bills with activity in the last N days. Stale bills
# rarely get new actions; re-fetching them daily wastes API budget and stretches
# the daily-update window. Hard cap as a safety net for cold-cache deploys.
ACTIVE_WINDOW_DAYS = 7
MAX_BILLS_PER_PULL = 200


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
class CongressBillActionsAdapter(Adapter):
    name = "congress_bill_actions"
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

    async def pull(self) -> AsyncIterator[Event]:
        # Pull list of recently-active bills from our local bills table —
        # don't go back to the API to discover bills, that's bills adapter's job.
        rows = self.store.conn.execute(
            """
            SELECT bill_id, congress, bill_type, number
            FROM bills
            WHERE COALESCE(latest_action_date, introduced_date)
                  >= CURRENT_DATE - INTERVAL (?) DAY
            ORDER BY COALESCE(latest_action_date, introduced_date) DESC NULLS LAST
            LIMIT ?
            """,
            [ACTIVE_WINDOW_DAYS, MAX_BILLS_PER_PULL],
        ).fetchall()

        for bill_id, congress, bill_type, number in rows:
            try:
                async for ev in self._actions_for(bill_id, congress, bill_type, number):
                    yield ev
            except httpx.HTTPError as e:
                logger.warning("skip actions for %s on HTTP error: %s", bill_id, e)
                continue
            await asyncio.sleep(0.05)

    async def _actions_for(
        self, bill_id: str, congress: int, bill_type: str, number: int,
    ) -> AsyncIterator[Event]:
        offset = 0
        bill_entity = f"bill:{bill_id}"
        while True:
            data = await self._get_json(
                f"/bill/{congress}/{bill_type}/{number}/actions",
                params={"limit": ACTIONS_LIMIT, "offset": offset},
            )
            actions = data.get("actions") or []
            if not actions:
                break
            for a in actions:
                action_date = _parse_dt(
                    (a.get("actionDate") + "T12:00:00Z") if a.get("actionDate") else None
                )
                action_code = a.get("actionCode") or ""
                action_type = a.get("type") or ""
                source_id = (
                    f"action:{bill_id}:{a.get('actionDate')}:{action_code or action_type}:"
                    f"{(a.get('text') or '')[:48]}"
                )
                payload = {
                    "bill_id": bill_id,
                    "congress": str(congress),
                    "bill_type": bill_type,
                    "number": number,
                    "action_date": a.get("actionDate"),
                    "action_code": action_code,
                    "action_type": action_type,
                    "source_system": (a.get("sourceSystem") or {}).get("name"),
                    "text": a.get("text") or "",
                    "committees": [
                        c.get("name") for c in (a.get("committees") or []) if c.get("name")
                    ],
                    "url": f"https://www.congress.gov/bill/{congress}th-congress/{bill_type}/{number}/all-actions",
                }
                yield Event.build(
                    source=self.name,
                    source_id=source_id,
                    entity_id=bill_entity,
                    event_type="bill.action",
                    payload=payload,
                    occurred_at=action_date,
                    schema_version=self.schema_version,
                )
            offset += ACTIONS_LIMIT
            if len(actions) < ACTIONS_LIMIT:
                break
