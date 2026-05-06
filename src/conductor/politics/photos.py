"""Photo URL resolver with fallback chain.

Primary: @unitedstates/images on GitHub gh-pages (community-maintained).
Fallback: api.congress.gov member endpoint -> depiction.imageUrl
         (newer members the gh-pages repo hasn't picked up yet).

Cache is persisted in a `photo_cache` DuckDB table so resolution survives
restarts (web on Railway etc.). In-memory dict mirrors the table for hot
reads. The web app exposes /photo/{bioguide} which 302-redirects to the
resolved URL.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional

import httpx

from conductor.secrets import get as get_secret

logger = logging.getLogger(__name__)

UNITEDSTATES_BASE = "https://raw.githubusercontent.com/unitedstates/images/gh-pages/congress"
PLACEHOLDER = "PLACEHOLDER"  # sentinel — web app substitutes inline SVG

_lock = threading.Lock()
_resolved: dict[tuple[str, str], str] = {}   # (bioguide, size) -> resolved URL or PLACEHOLDER

PHOTO_CACHE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS photo_cache (
    bioguide_id  VARCHAR NOT NULL,
    size         VARCHAR NOT NULL,
    url          VARCHAR,            -- NULL => placeholder/missing
    source       VARCHAR,            -- 'unitedstates' | 'congress_gov' | 'placeholder'
    fetched_at   TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (bioguide_id, size)
);
"""


def ensure_schema(store) -> None:
    if getattr(store, "read_only", False):
        return
    store.conn.execute(PHOTO_CACHE_SCHEMA_SQL)


def _db_load_into_memory(store) -> int:
    """Populate the in-memory cache from the DB table on startup.

    Skip rows where `url IS NULL` (recorded placeholders). When a legislator
    first appeared without a photo, we persisted a placeholder row so future
    requests would short-circuit. That broke when those legislators later
    got photos in unitedstates/images: the cache row stuck the bioguide on
    PLACEHOLDER forever, and warm_cache skipped re-checking because the
    in-memory cache was hot. Skipping placeholder rows on load lets
    warm_cache (or the next request's resolve()) probe the network again.
    """
    ensure_schema(store)
    rows = store.conn.execute(
        "SELECT bioguide_id, size, url FROM photo_cache WHERE url IS NOT NULL"
    ).fetchall()
    n = 0
    with _lock:
        for bg, sz, url in rows:
            _resolved[(bg, sz)] = url
            n += 1
    return n


def _db_persist(store, bioguide: str, size: str, url: str | None, source: str) -> None:
    ensure_schema(store)
    store.conn.execute(
        """
        INSERT INTO photo_cache (bioguide_id, size, url, source, fetched_at)
        VALUES (?, ?, ?, ?, NOW())
        ON CONFLICT (bioguide_id, size) DO UPDATE SET
            url = excluded.url,
            source = excluded.source,
            fetched_at = excluded.fetched_at
        """,
        [bioguide, size, url, source],
    )


def static_unitedstates_url(bioguide_id: str, size: str = "225x275") -> str:
    return f"{UNITEDSTATES_BASE}/{size}/{bioguide_id}.jpg"


def photo_url(bioguide_id: str, size: str = "225x275") -> str:
    """Template helper — renders to /photo/{bioguide} which the web app resolves."""
    return f"/photo/{bioguide_id}?size={size}"


def _check_unitedstates_sync(bioguide_id: str, size: str) -> Optional[str]:
    url = static_unitedstates_url(bioguide_id, size)
    try:
        with httpx.Client(timeout=8.0, follow_redirects=True) as c:
            r = c.head(url)
            if r.status_code == 200:
                return url
    except httpx.HTTPError as e:
        logger.debug("unitedstates HEAD failed %s: %s", bioguide_id, e)
    return None


def _check_api_congress_sync(bioguide_id: str) -> Optional[str]:
    api_key = get_secret("CONGRESS_GOV_API_KEY")
    if not api_key:
        return None
    try:
        with httpx.Client(timeout=10.0, follow_redirects=True) as c:
            r = c.get(
                f"https://api.congress.gov/v3/member/{bioguide_id}",
                params={"format": "json"},
                headers={"X-Api-Key": api_key},
            )
            if r.status_code != 200:
                return None
            data = r.json()
            return ((data.get("member") or {}).get("depiction") or {}).get("imageUrl")
    except (httpx.HTTPError, ValueError) as e:
        logger.debug("api.congress.gov member fetch failed %s: %s", bioguide_id, e)
        return None


def resolve(bioguide_id: str, size: str = "225x275", *, store=None) -> str:
    """Resolve bioguide -> best photo URL. Cached in memory + DB; returns
    PLACEHOLDER sentinel when no source has the image. Pass `store` to
    persist the result; safe to omit (memory only)."""
    if not bioguide_id or not bioguide_id[0].isalpha():
        return PLACEHOLDER

    key = (bioguide_id, size)
    with _lock:
        if key in _resolved:
            return _resolved[key]

    us = _check_unitedstates_sync(bioguide_id, size)
    if us:
        with _lock:
            _resolved[key] = us
        if store is not None:
            try: _db_persist(store, bioguide_id, size, us, "unitedstates")
            except Exception: pass
        return us

    api_url = _check_api_congress_sync(bioguide_id)
    if api_url:
        with _lock:
            _resolved[key] = api_url
        if store is not None:
            try: _db_persist(store, bioguide_id, size, api_url, "congress_gov")
            except Exception: pass
        return api_url

    with _lock:
        _resolved[key] = PLACEHOLDER
    if store is not None:
        try: _db_persist(store, bioguide_id, size, None, "placeholder")
        except Exception: pass
    return PLACEHOLDER


async def warm_cache(
    bioguide_ids: list[str],
    size: str = "225x275",
    concurrency: int = 24,
    store=None,
) -> dict[str, int]:
    """Pre-resolve photos in parallel. Populates the in-memory cache and,
    when `store` is provided, persists each result to `photo_cache` table
    so subsequent restarts (Railway etc.) skip the network entirely.

    Skips bioguides already cached in memory (which includes anything
    loaded from DB on startup via load_persisted_into_memory).
    """
    counts = {"unitedstates": 0, "api": 0, "missing": 0, "cached": 0}
    sem = asyncio.Semaphore(concurrency)
    api_key = get_secret("CONGRESS_GOV_API_KEY")

    async def _one(bg: str, client: httpx.AsyncClient):
        key = (bg, size)
        with _lock:
            if key in _resolved:
                counts["cached"] += 1
                return
        async with sem:
            us_url = static_unitedstates_url(bg, size)
            try:
                r = await client.head(us_url)
                if r.status_code == 200:
                    with _lock:
                        _resolved[key] = us_url
                    if store is not None:
                        try: _db_persist(store, bg, size, us_url, "unitedstates")
                        except Exception: pass
                    counts["unitedstates"] += 1
                    return
            except httpx.HTTPError:
                pass
            if api_key:
                try:
                    r = await client.get(
                        f"https://api.congress.gov/v3/member/{bg}",
                        params={"format": "json"},
                        headers={"X-Api-Key": api_key},
                    )
                    if r.status_code == 200:
                        d = r.json()
                        url = ((d.get("member") or {}).get("depiction") or {}).get("imageUrl")
                        if url:
                            with _lock:
                                _resolved[key] = url
                            if store is not None:
                                try: _db_persist(store, bg, size, url, "congress_gov")
                                except Exception: pass
                            counts["api"] += 1
                            return
                except (httpx.HTTPError, ValueError):
                    pass
            with _lock:
                _resolved[key] = PLACEHOLDER
            if store is not None:
                try: _db_persist(store, bg, size, None, "placeholder")
                except Exception: pass
            counts["missing"] += 1

    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        await asyncio.gather(*(_one(bg, client) for bg in bioguide_ids))
    return counts


def load_persisted_into_memory(store) -> int:
    """Public helper — call once on web startup to seed the in-memory cache
    from the DB table. Returns count of rows loaded."""
    return _db_load_into_memory(store)
