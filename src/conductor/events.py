"""Canonical event model for the Conductor event store.

Every adapter emits Events. The store is append-only — same (source, source_id)
over time produces multiple Events whenever payload_hash changes.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


def _now_utc() -> datetime:
    return datetime.now(tz=timezone.utc)


def hash_payload(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


class Event(BaseModel):
    """Append-only event in the Conductor store.

    source        - adapter slug, e.g. "steam_featured"
    source_id     - globally-unique-within-source identifier
    entity_id     - cross-source entity reference, e.g. "steam:app:730"
    event_type    - dotted noun.verb, e.g. "app.discovered", "news.published"
    observed_at   - when Conductor saw it
    occurred_at   - source-claimed timestamp; falls back to observed_at
    payload_hash  - sha256(payload) prefix for delta detection
    payload       - canonical normalized payload
    schema_version- adapter schema version for migration
    """

    source: str
    source_id: str
    entity_id: str
    event_type: str
    observed_at: datetime = Field(default_factory=_now_utc)
    occurred_at: datetime
    payload_hash: str
    payload: dict[str, Any]
    schema_version: int = 1

    @classmethod
    def build(
        cls,
        *,
        source: str,
        source_id: str,
        entity_id: str,
        event_type: str,
        payload: dict[str, Any],
        occurred_at: datetime | None = None,
        schema_version: int = 1,
    ) -> "Event":
        observed = _now_utc()
        return cls(
            source=source,
            source_id=source_id,
            entity_id=entity_id,
            event_type=event_type,
            observed_at=observed,
            occurred_at=occurred_at or observed,
            payload_hash=hash_payload(payload),
            payload=payload,
            schema_version=schema_version,
        )
