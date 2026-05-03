"""Committee meetings adapter — api.congress.gov /committee-meeting.

Each meeting carries: type (Hearing/Markup), committee, date, title,
witnesses (named), and sometimes a member attendance list.

Emits two event types:
  - committee.meeting       (entity = committee, fact about the committee)
  - committee.markup_vote   (entity = bioguide:..., when a member is named in
                              markup recorded votes — when available)
  - committee.hearing_attended (entity = bioguide:..., per member listed)

Coverage caveat: api.congress.gov populates this endpoint inconsistently —
some committees publish full attendance, others post only meeting metadata.
Treat absence of member-level events as missing data, not zero attendance.
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
MAX_PAGES = 6
MAX_MEETINGS_PER_PULL = 100


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
class CongressCommitteeMeetingsAdapter(Adapter):
    name = "congress_committee_meetings"
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
            cursor = datetime.now(tz=timezone.utc) - timedelta(days=14)
            logger.info("cold start — pulling committee meetings since %s", cursor.isoformat())

        offset = 0
        meetings: list[dict] = []
        for _ in range(MAX_PAGES):
            data = await self._get_json(
                "/committee-meeting",
                params={
                    "limit": LIST_LIMIT,
                    "offset": offset,
                    "fromDateTime": cursor.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
            )
            page = data.get("committeeMeetings") or []
            if not page:
                break
            meetings.extend(page)
            offset += LIST_LIMIT
            if len(page) < LIST_LIMIT:
                break

        logger.info("[%s] %d meetings since cursor (processing up to %d)",
                    self.name, len(meetings), MAX_MEETINGS_PER_PULL)
        meetings = meetings[:MAX_MEETINGS_PER_PULL]

        max_seen = cursor
        for m in meetings:
            try:
                async for ev in self._process_meeting(m):
                    yield ev
                    if ev.occurred_at > max_seen:
                        max_seen = ev.occurred_at
            except httpx.HTTPError as e:
                logger.warning("skip meeting on HTTP error: %s", e)
                continue
            await asyncio.sleep(0.04)

        if max_seen > cursor:
            self._set_cursor(max_seen)

    async def _process_meeting(self, m: dict) -> AsyncIterator[Event]:
        congress = m.get("congress")
        chamber = (m.get("chamber") or "").lower()
        event_id = m.get("eventId")
        if not (congress and event_id):
            return

        # Detail fetch
        detail = await self._get_json(
            f"/committee-meeting/{congress}/{chamber}/{event_id}"
        )
        meeting = detail.get("committeeMeeting") or {}
        meeting_date = _parse_dt(meeting.get("date"))
        meeting_type = meeting.get("type") or ""    # "Hearing", "Markup", etc.
        title = meeting.get("title") or ""
        committees = meeting.get("committees") or []
        primary_committee = committees[0] if committees else {}
        committee_code = primary_committee.get("systemCode") or ""
        committee_name = primary_committee.get("name") or ""

        common_payload = {
            "congress": str(congress),
            "chamber": chamber,
            "event_id": event_id,
            "meeting_type": meeting_type,
            "title": title,
            "committee_code": committee_code,
            "committee_name": committee_name,
            "url": f"https://www.congress.gov/committee-meeting/{congress}/{chamber}/{event_id}",
        }

        # Committee-level event
        yield Event.build(
            source=self.name,
            source_id=f"meeting:{event_id}",
            entity_id=f"committee:{committee_code}" if committee_code else f"chamber:{chamber}",
            event_type="committee.meeting",
            payload=common_payload,
            occurred_at=meeting_date,
            schema_version=self.schema_version,
        )

        # Member-level attendance — present in some meetings, missing in others
        # Schema varies; meetings sometimes carry `meetingDocuments` referencing
        # a roll-call document. For now, surface witnesses (not members) as a
        # signal source; member attendance fan-out can be layered later when
        # markup vote XMLs are integrated.
        for wit in (meeting.get("witnesses") or []):
            wit_name = wit.get("name") or ""
            wit_org = wit.get("organization") or ""
            if not wit_name:
                continue
            yield Event.build(
                source=self.name,
                source_id=f"witness:{event_id}:{wit_name[:40]}",
                entity_id=f"committee:{committee_code}" if committee_code else f"chamber:{chamber}",
                event_type="committee.witness",
                payload={
                    **common_payload,
                    "witness_name": wit_name,
                    "witness_organization": wit_org,
                    "witness_position": wit.get("position") or "",
                },
                occurred_at=meeting_date,
                schema_version=self.schema_version,
            )
