"""Adapter base class + registry.

Adapters declare static metadata (name, schema_version, cadence) and implement
async `pull` (and optionally `enrich`). Each yields Events into the store.
"""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import timedelta
from typing import AsyncIterator, ClassVar

from conductor.events import Event
from conductor.http import HttpClient
from conductor.store import Store

logger = logging.getLogger(__name__)


class Adapter(ABC):
    name: ClassVar[str]
    schema_version: ClassVar[int] = 1
    cadence: ClassVar[timedelta] = timedelta(days=1)

    def __init__(self, store: Store, http: HttpClient):
        self.store = store
        self.http = http

    @abstractmethod
    async def pull(self) -> AsyncIterator[Event]:
        """Yield events from the source. Implementations decide cursor handling."""
        if False:
            yield  # pragma: no cover - declares this is an async generator

    async def enrich(self, entity_id: str) -> AsyncIterator[Event]:
        """Optional: deep-fetch a single entity. Used by enrichment adapters."""
        if False:
            yield  # pragma: no cover
        return

    async def run_pull(self) -> int:
        """Drain pull() into the store. Returns number of new events written."""
        events: list[Event] = []
        async for ev in self.pull():
            events.append(ev)
        n = self.store.insert_events(events)
        logger.info("[%s] pulled=%d new=%d", self.name, len(events), n)
        return n

    async def run_enrich(self, entity_ids: list[str]) -> int:
        events: list[Event] = []
        for eid in entity_ids:
            async for ev in self.enrich(eid):
                events.append(ev)
        n = self.store.insert_events(events)
        logger.info("[%s] enriched %d entities, new events=%d", self.name, len(entity_ids), n)
        return n


class AdapterRegistry:
    def __init__(self):
        self._registry: dict[str, type[Adapter]] = {}

    def register(self, cls: type[Adapter]) -> type[Adapter]:
        self._registry[cls.name] = cls
        return cls

    def get(self, name: str) -> type[Adapter]:
        if name not in self._registry:
            raise KeyError(f"unknown adapter: {name} (have: {sorted(self._registry)})")
        return self._registry[name]

    def names(self) -> list[str]:
        return sorted(self._registry)


registry = AdapterRegistry()
