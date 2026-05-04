"""api.congress.gov bill summaries adapter.

CRS (Congressional Research Service) writes neutral, nonpartisan summaries
of bills as they progress through stages. We pull them per-bill and store in
`bill_summaries` for display under each bill's text version.

Endpoint: GET /v3/bill/{congress}/{billType}/{billNumber}/summaries

Each entry has:
  - actionDate     (when this summary version applies)
  - actionDesc     ("Introduced in House", "Reported to House", ...)
  - text           (HTML — strip to plain text)
  - versionCode    (matches our text version_code, e.g. "ih", "rh")
  - updateDate

We map versionCode → bill text's version_code so the changelog UI can show
the CRS summary alongside the matching diff.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from typing import AsyncIterator

import httpx

from conductor.adapters.base import Adapter, registry
from conductor.events import Event
from conductor.politics.bill_text import clean_html_to_lines
from conductor.secrets import require

logger = logging.getLogger(__name__)

API_BASE = "https://api.congress.gov/v3"
PER_BILL_SLEEP = 0.05   # rate-limit hygiene
MAX_BILLS_PER_PULL = 50_000  # uncapped in practice; ~15k bills/Congress total

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

    async def pull(self) -> AsyncIterator[Event]:
        ensure_schema(self.store)

        # Skip bills whose latest stored summary is newer than the bill's own
        # `updated_at` — no upstream change since we last fetched, so no work.
        # First-time runs hit every bill (no rows in bill_summaries yet).
        bills = self.store.conn.execute(
            """
            SELECT b.bill_id, b.congress, b.bill_type, b.number, b.latest_action_date
            FROM bills b
            LEFT JOIN (
                SELECT bill_id, MAX(update_date) AS last_seen
                FROM bill_summaries
                GROUP BY bill_id
            ) s ON s.bill_id = b.bill_id
            WHERE s.last_seen IS NULL
               OR s.last_seen < b.updated_at
            ORDER BY COALESCE(b.latest_action_date, b.introduced_date) DESC NULLS LAST
            LIMIT ?
            """,
            [MAX_BILLS_PER_PULL],
        ).fetchall()

        logger.info("[%s] starting — %d bills in queue", self.name, len(bills))
        upserted_total = 0
        for i, (bill_id, congress, bill_type, number, latest_action) in enumerate(bills):
            try:
                upserted = await self._pull_one(bill_id, congress, bill_type, number)
            except httpx.HTTPError as e:
                logger.warning("[%s] %s: HTTP %s", self.name, bill_id, e)
                continue
            upserted_total += upserted
            if (i + 1) % 100 == 0:
                logger.info("[%s] progress %d/%d — upserted=%d",
                            self.name, i + 1, len(bills), upserted_total)
            await asyncio.sleep(PER_BILL_SLEEP)

        logger.info("[%s] DONE — upserted %d summary rows", self.name, upserted_total)

        if False:
            yield  # pragma: no cover

    async def _pull_one(self, bill_id: str, congress, bill_type, number) -> int:
        data = await self._get_json(
            f"/bill/{congress}/{bill_type}/{number}/summaries"
        )
        summaries = data.get("summaries") or []
        upserted = 0
        for s in summaries:
            version_code = (s.get("versionCode") or "").lower()
            action_date = _parse_date(s.get("actionDate"))
            text_html = s.get("text") or ""
            if not (version_code and action_date and text_html):
                continue
            lines = clean_html_to_lines(text_html)
            text = "\n".join(lines).strip()
            if not text:
                continue
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
                    _parse_dt(s.get("updateDate")),
                ],
            )
            upserted += 1
        return upserted


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
