"""api.congress.gov bill summaries adapter.

CRS (Congressional Research Service) writes neutral, nonpartisan summaries
of bills as they progress through stages. We pull the global summaries feed
filtered by `fromDateTime` so each run only sees what changed upstream since
the last cursor, regardless of how many bills exist.

Endpoint: GET /v3/summaries?fromDateTime=<ISO>&sort=updateDate+asc

Each entry has:
  - bill           ({congress, type, number, ...})
  - actionDate     (when this summary version applies)
  - actionDesc     ("Introduced in House", "Reported to House", ...)
  - text           (HTML — strip to plain text)
  - versionCode    (matches our text version_code, e.g. "ih", "rh")
  - updateDate     (drives the cursor)

Cold-start cursor: 2026-05-02T00:00:00Z. The seed DB shipped via
GitHub Release contains every prior summary, so the live adapter only
needs to chase the delta from that floor forward.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import AsyncIterator

import httpx

from conductor.adapters.base import Adapter, registry
from conductor.events import Event
from conductor.politics.bill_text import clean_html_to_lines
from conductor.secrets import require

logger = logging.getLogger(__name__)

API_BASE = "https://api.congress.gov/v3"
PAGE_LIMIT = 250                       # api.congress.gov hard cap
COLD_START_FLOOR = datetime(2026, 5, 2, tzinfo=timezone.utc)

BILL_SUMMARIES_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bill_summaries (
    bill_id        VARCHAR,
    version_code   VARCHAR,           -- "ih", "rh", "enr", ...
    action_desc    VARCHAR,           -- "Introduced in House", etc.
    action_date    DATE,
    summary_text   VARCHAR,           -- plain text, HTML stripped
    update_date    TIMESTAMPTZ,
    PRIMARY KEY (bill_id, version_code, action_date)
);
CREATE INDEX IF NOT EXISTS idx_bill_summaries_bill ON bill_summaries(bill_id);
"""


def ensure_schema(store) -> None:
    if getattr(store, "read_only", False):
        return
    store.conn.execute(BILL_SUMMARIES_SCHEMA_SQL)


@registry.register
class CongressBillSummariesAdapter(Adapter):
    name = "congress_bill_summaries"
    schema_version = 1

    @property
    def api_key(self) -> str:
        return require("CONGRESS_GOV_API_KEY")

    async def _get_json(self, path: str, params: dict | None = None) -> dict:
        params = dict(params or {})
        params["format"] = "json"
        url = f"{API_BASE}{path}"
        r = await self.http.get(
            url, params=params, headers={"X-Api-Key": self.api_key},
        )
        if r.status_code == 404:
            return {}
        r.raise_for_status()
        return r.json()

    def _cursor_dt(self) -> datetime:
        raw = self.store.get_cursor(self.name)
        if raw:
            try:
                dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except ValueError:
                pass
        return COLD_START_FLOOR

    def _set_cursor_dt(self, dt: datetime) -> None:
        self.store.set_cursor(self.name, dt.astimezone(timezone.utc).isoformat())

    async def pull(self) -> AsyncIterator[Event]:
        ensure_schema(self.store)

        cursor = self._cursor_dt()
        from_str = cursor.strftime("%Y-%m-%dT%H:%M:%SZ")
        logger.info("[%s] starting — fromDateTime=%s", self.name, from_str)

        offset = 0
        upserted_total = 0
        max_seen = cursor
        # Bound the walk so a runaway / clock-skew page loop can't peg the
        # API. Daily delta should fit in a handful of pages.
        MAX_PAGES = 200

        for _page in range(MAX_PAGES):
            try:
                data = await self._get_json(
                    "/summaries",
                    params={
                        "fromDateTime": from_str,
                        "sort": "updateDate asc",
                        "limit": PAGE_LIMIT,
                        "offset": offset,
                    },
                )
            except httpx.HTTPError as e:
                logger.warning("[%s] page offset=%d: HTTP %s", self.name, offset, e)
                break

            items = data.get("summaries") or []
            if not items:
                break

            page_upserted = 0
            page_max: datetime | None = None
            for s in items:
                bill = s.get("bill") or {}
                congress = bill.get("congress")
                bill_type = (bill.get("type") or "").lower()
                number = bill.get("number")
                if not (congress and bill_type and number):
                    continue
                bill_id = f"{congress}:{bill_type}:{number}"

                version_code = (s.get("versionCode") or "").lower()
                action_date = _parse_date(s.get("actionDate"))
                text_html = s.get("text") or ""
                if not (version_code and action_date and text_html):
                    continue
                text = "\n".join(clean_html_to_lines(text_html)).strip()
                if not text:
                    continue

                update_dt = _parse_dt(s.get("updateDate"))
                self.store.conn.execute(
                    """
                    INSERT INTO bill_summaries
                        (bill_id, version_code, action_desc, action_date, summary_text, update_date)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT (bill_id, version_code, action_date) DO UPDATE SET
                        action_desc  = excluded.action_desc,
                        summary_text = excluded.summary_text,
                        update_date  = excluded.update_date
                    """,
                    [
                        bill_id,
                        version_code,
                        s.get("actionDesc") or "",
                        action_date,
                        text,
                        update_dt,
                    ],
                )
                page_upserted += 1
                if update_dt is not None and (page_max is None or update_dt > page_max):
                    page_max = update_dt

            upserted_total += page_upserted

            # Advance cursor per page so a kill is bounded to one page.
            # Re-fetch from the same boundary on resume is safe — UPSERT
            # handles overlap.
            if page_max is not None and page_max > max_seen:
                max_seen = page_max
                self._set_cursor_dt(max_seen)

            logger.info(
                "[%s] page offset=%d items=%d upserted=%d cursor=%s",
                self.name, offset, len(items), page_upserted,
                max_seen.isoformat(),
            )

            if len(items) < PAGE_LIMIT:
                break
            offset += PAGE_LIMIT
        else:
            logger.warning("[%s] hit MAX_PAGES=%d; cursor=%s — investigate",
                           self.name, MAX_PAGES, max_seen.isoformat())

        logger.info("[%s] DONE — upserted %d summary rows; cursor=%s",
                    self.name, upserted_total, max_seen.isoformat())

        if False:
            yield  # pragma: no cover


def _parse_date(s: str | None):
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_dt(s: str | None):
    if not s:
        return None
    s = str(s).replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
