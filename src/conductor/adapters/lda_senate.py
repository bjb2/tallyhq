"""Senate LDA filings adapter.

Source: https://lda.senate.gov/api/v1/ — Lobbying Disclosure Act filings.
Free; optional API key (env: LDA_API_KEY) raises rate limit 25→100 rps.

Pagination: page=N, page_size capped server-side at 25. Walk the `next` URL
until null. Cursor stored per (year, period) tuple, value = last page processed.

Per filing we emit:
  - lda_filing                       (entity: lda_filing:{uuid})
  - bill_lobbied (per resolved bill) (entity: bill:{bill_id})

Bill resolution: regex over lobbying_activities[].description. We DO NOT
gate on the existence of the bill in the bills table — emit even if unresolved
so a later bills load can backfill. We log resolution rate for visibility.

`client_lobbied` is intentionally NOT emitted: low signal vs. the per-filing
event already keyed on client_id, and would 4x the event volume.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import AsyncIterator, Iterable

import httpx

from conductor.adapters.base import Adapter, registry
from conductor.events import Event
from conductor.politics import bills as bills_mod
from conductor.politics import lobbying as lda_mod
from conductor.secrets import get as secret_get

logger = logging.getLogger(__name__)

API_BASE = "https://lda.gov/api/v1"  # successor host; senate.gov sunsets 2026-06-30

PERIODS = (
    "first_quarter",
    "second_quarter",
    "third_quarter",
    "fourth_quarter",
)


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _years_for_congress(congress: int) -> tuple[int, int]:
    # 119th = 2025-2026
    base = 2025 + (congress - 119) * 2
    return (base, base + 1)


@registry.register
class LdaSenateAdapter(Adapter):
    name = "lda_senate"
    # v2 adds `mention_text` to bill_lobbied payloads for match-confidence
    # scoring. Older v1 events lack the field; lobby_match handles that
    # gracefully via degraded-mode scoring.
    schema_version = 2

    # Default scope = current congress. Override via class-level set_scope().
    years: tuple[int, ...] = (2025, 2026)
    periods: tuple[str, ...] = PERIODS
    sleep_per_page: float = 0.05
    max_pages_per_period: int | None = None  # None = drain
    max_retries: int = 12

    @property
    def api_key(self) -> str | None:
        return secret_get("LDA_API_KEY")

    async def run_pull(self) -> int:
        """Override base.run_pull so we flush events in batches.

        Backfilling all of Congress 119 = ~250K filings × ~1-3 events each.
        The base implementation collects everything into a list before insert,
        which is fine for a normal pull but will balloon RAM here.
        """
        BATCH = 1000
        buf: list[Event] = []
        total_yielded = 0
        total_new = 0
        async for ev in self.pull():
            buf.append(ev)
            total_yielded += 1
            if len(buf) >= BATCH:
                total_new += self.store.insert_events(buf)
                buf.clear()
        if buf:
            total_new += self.store.insert_events(buf)
        logger.info("[%s] pulled=%d new=%d", self.name, total_yielded, total_new)
        if hasattr(self, "_stats"):
            s = self._stats
            rate = (s["resolved"] / s["refs"] * 100) if s["refs"] else 0.0
            logger.info(
                "[%s] bill refs: %d total, %d resolved (%.1f%%), %d filings with refs",
                self.name, s["refs"], s["resolved"], rate, s["filings_with_refs"],
            )
        return total_new

    def _headers(self) -> dict:
        h = {"Accept": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Token {self.api_key}"
        return h

    async def _get_json(self, url: str, params: dict | None = None) -> dict:
        client = self.http._client
        if client is None:
            raise RuntimeError("HttpClient not entered")
        # Manual retry on 429 / 5xx. LDA's anonymous tier rate-limits aggressively
        # and returns Retry-After. Honor it; fall back to exponential.
        backoff = 1.0
        for attempt in range(self.max_retries):
            try:
                r = await client.get(url, params=params, headers=self._headers())
            except httpx.TransportError as e:
                logger.warning("LDA transport error: %s (attempt %d)", e, attempt + 1)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60.0)
                continue
            if r.status_code == 429 or r.status_code >= 500:
                ra = r.headers.get("Retry-After")
                wait: float
                if ra:
                    try:
                        wait = float(ra)
                    except ValueError:
                        wait = backoff
                else:
                    wait = backoff
                wait = max(wait, 1.0)
                logger.warning("LDA %s -> %s, waiting %.1fs (attempt %d)",
                               url, r.status_code, wait, attempt + 1)
                await asyncio.sleep(wait)
                backoff = min(backoff * 2, 60.0)
                continue
            if r.status_code >= 400:
                r.raise_for_status()
            return r.json()
        raise RuntimeError(f"LDA giving up after {self.max_retries} attempts: {url}")

    def _cursor_key(self, year: int, period: str) -> str:
        return f"{self.name}:{year}:{period}"

    def _get_period_cursor(self, year: int, period: str) -> int:
        raw = self.store.get_cursor(self._cursor_key(year, period))
        try:
            return int(raw) if raw else 0
        except ValueError:
            return 0

    def _set_period_cursor(self, year: int, period: str, last_page: int) -> None:
        self.store.set_cursor(self._cursor_key(year, period), str(last_page))

    async def pull(self) -> AsyncIterator[Event]:
        lda_mod.ensure_schema(self.store)
        bills_mod.ensure_schema(self.store)

        for year in self.years:
            for period in self.periods:
                async for ev in self._pull_period(year, period):
                    yield ev

    async def _pull_period(self, year: int, period: str) -> AsyncIterator[Event]:
        start_page = self._get_period_cursor(year, period) + 1
        page = start_page
        total_pages_seen = 0
        total_filings = 0
        url: str | None = (
            f"{API_BASE}/filings/?filing_year={year}&filing_period={period}&page={page}"
        )

        logger.info("[%s] %d %s — starting at page %d", self.name, year, period, page)

        while url:
            try:
                data = await self._get_json(url)
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    logger.info("[%s] %d %s — page %d 404, end", self.name, year, period, page)
                    break
                logger.warning("[%s] HTTP error: %s — stopping period", self.name, e)
                break

            results = data.get("results") or []
            if not results:
                logger.info("[%s] %d %s — empty page %d, end", self.name, year, period, page)
                break

            count = data.get("count")
            if total_pages_seen == 0 and count is not None:
                logger.info("[%s] %d %s — count=%d", self.name, year, period, count)

            for filing in results:
                async for ev in self._emit_filing(filing, year, period):
                    yield ev
                total_filings += 1

            self._set_period_cursor(year, period, page)
            total_pages_seen += 1

            if total_pages_seen % 25 == 0:
                logger.info(
                    "[%s] %d %s — %d pages, %d filings so far",
                    self.name, year, period, total_pages_seen, total_filings,
                )

            if (
                self.max_pages_per_period is not None
                and total_pages_seen >= self.max_pages_per_period
            ):
                logger.info("[%s] hit max_pages_per_period=%d", self.name, self.max_pages_per_period)
                break

            next_url = data.get("next")
            if not next_url:
                break
            url = next_url
            page += 1
            await asyncio.sleep(self.sleep_per_page)

        logger.info(
            "[%s] %d %s — DONE: %d pages, %d filings",
            self.name, year, period, total_pages_seen, total_filings,
        )

    async def _emit_filing(
        self, filing: dict, year: int, period: str
    ) -> AsyncIterator[Event]:
        uuid = filing.get("filing_uuid")
        if not uuid:
            return

        registrant = filing.get("registrant") or {}
        client = filing.get("client") or {}
        activities = filing.get("lobbying_activities") or []

        # Aggregate bill refs across activities (and per-activity for bill_lobbied).
        # Track (issue_code, refs, description) so we can stash mention text
        # for downstream match-confidence scoring (see lobby_match.py).
        per_activity_refs: list[tuple[str, list[str], str]] = []
        all_refs: set[str] = set()
        issue_codes_global: set[str] = set()

        for act in activities:
            issue = act.get("general_issue_code") or ""
            desc = act.get("description") or ""
            if issue:
                issue_codes_global.add(issue)
            refs = lda_mod.extract_bill_refs(desc, year)
            per_activity_refs.append((issue, refs, desc))
            all_refs.update(refs)

        bill_refs = sorted(all_refs)
        issue_codes = sorted(issue_codes_global)

        income_raw = filing.get("income")
        expenses_raw = filing.get("expenses")
        try:
            income = float(income_raw) if income_raw not in (None, "") else None
        except (ValueError, TypeError):
            income = None
        try:
            expenses = float(expenses_raw) if expenses_raw not in (None, "") else None
        except (ValueError, TypeError):
            expenses = None

        dt_posted = _parse_dt(filing.get("dt_posted"))
        occurred = dt_posted or datetime.now(tz=timezone.utc)

        registrant_id = str(registrant.get("id")) if registrant.get("id") is not None else None
        registrant_name = registrant.get("name") or filing.get("registrant_name") or ""
        client_id = str(client.get("id")) if client.get("id") is not None else None
        client_name = client.get("name") or ""
        raw_url = filing.get("filing_document_url") or filing.get("url") or ""

        # Upsert entity row
        lda_mod.upsert(
            self.store,
            lda_mod.LdaFiling(
                filing_uuid=uuid,
                filing_year=int(filing.get("filing_year") or year),
                filing_period=filing.get("filing_period") or period,
                dt_posted=dt_posted,
                income=income,
                expenses=expenses,
                registrant_id=registrant_id,
                registrant_name=registrant_name,
                client_id=client_id,
                client_name=client_name,
                activity_count=len(activities),
                issue_codes=issue_codes,
                bill_refs=bill_refs,
                raw_url=raw_url,
            ),
        )

        # Bill resolution rate logging (counters live on the adapter instance)
        if not hasattr(self, "_stats"):
            self._stats = {"refs": 0, "resolved": 0, "filings_with_refs": 0}
        if bill_refs:
            self._stats["filings_with_refs"] += 1
        for ref in bill_refs:
            self._stats["refs"] += 1
            if bills_mod.get(self.store, ref) is not None:
                self._stats["resolved"] += 1

        # 1) lda_filing event
        filing_payload = {
            "filing_uuid": uuid,
            "filing_type": filing.get("filing_type"),
            "filing_type_display": filing.get("filing_type_display"),
            "filing_year": filing.get("filing_year"),
            "filing_period": filing.get("filing_period"),
            "dt_posted": filing.get("dt_posted"),
            "income": income,
            "expenses": expenses,
            "registrant_id": registrant_id,
            "registrant_name": registrant_name,
            "client_id": client_id,
            "client_name": client_name,
            "activity_count": len(activities),
            "issue_codes": issue_codes,
            "bill_refs": bill_refs,
            "raw_url": raw_url,
        }
        yield Event.build(
            source=self.name,
            source_id=uuid,
            entity_id=f"lda_filing:{uuid}",
            event_type="lda_filing",
            payload=filing_payload,
            occurred_at=occurred,
            schema_version=self.schema_version,
        )

        # 2) bill_lobbied per resolved bill mention
        if not bill_refs:
            return

        # amount_share = income / N bills (simple proxy). expenses fallback.
        amount_pool = income if income is not None else (expenses if expenses is not None else 0.0)
        share = amount_pool / len(bill_refs) if bill_refs else 0.0

        # Per-bill issue_codes = the issue codes of activities that referenced it
        for bill_id in bill_refs:
            bill_issues = sorted({
                issue for (issue, refs, _desc) in per_activity_refs
                if issue and bill_id in refs
            })
            # Concatenate descriptions of activities that mentioned this bill,
            # truncated for storage. Used by lobby_match for title-overlap
            # scoring on the read path.
            mentions = [
                desc for (_issue, refs, desc) in per_activity_refs
                if desc and bill_id in refs
            ]
            mention_text = " | ".join(mentions)[:1500]
            bill_resolved = bills_mod.get(self.store, bill_id) is not None
            payload = {
                "bill_id": bill_id,
                "bill_id_resolved": bill_resolved,
                "filing_uuid": uuid,
                "registrant_id": registrant_id,
                "registrant_name": registrant_name,
                "client_id": client_id,
                "client_name": client_name,
                "filing_year": filing.get("filing_year"),
                "filing_period": filing.get("filing_period"),
                "issue_codes_for_bill": bill_issues,
                "mention_text": mention_text,
                "amount_share": share,
                "raw_url": raw_url,
            }
            yield Event.build(
                source=self.name,
                source_id=f"bill_lobbied:{uuid}:{bill_id}",
                entity_id=f"bill:{bill_id}",
                event_type="bill_lobbied",
                payload=payload,
                occurred_at=occurred,
                schema_version=self.schema_version,
            )
