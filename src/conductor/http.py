"""Shared async HTTP client with retry."""
from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": "Conductor/0.1",
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
}


def _parse_retry_after(value: str | None) -> float | None:
    """Parse a Retry-After header. Returns seconds to wait, or None."""
    if not value:
        return None
    value = value.strip()
    # Integer seconds
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    # HTTP-date
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = (dt - datetime.now(tz=timezone.utc)).total_seconds()
    return max(0.0, delta) if delta > 0 else None

_RETRYABLE = (httpx.TransportError, httpx.HTTPStatusError)


class HttpClient:
    """Thin async wrapper around httpx.AsyncClient with sane defaults + retry."""

    def __init__(self, *, timeout: float = 30.0):
        self._client: httpx.AsyncClient | None = None
        self._timeout = timeout

    async def __aenter__(self):
        self._client = httpx.AsyncClient(
            headers=DEFAULT_HEADERS,
            follow_redirects=True,
            timeout=self._timeout,
        )
        return self

    async def __aexit__(self, *args):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        retry=retry_if_exception_type(_RETRYABLE),
        reraise=True,
    )
    async def get_json(self, url: str, params: dict | None = None) -> dict:
        assert self._client is not None, "Use as async context manager"
        logger.debug("GET %s params=%s", url, params)
        r = await self._client.get(url, params=params)
        if r.status_code >= 500 or r.status_code == 429:
            r.raise_for_status()
        if r.status_code >= 400:
            # 4xx other than 429 — don't retry, raise immediately
            r.raise_for_status()
        return r.json()

    async def get(self, url: str, *, params: dict | None = None,
                  headers: dict | None = None,
                  max_retries: int = 5,
                  max_backoff: float = 60.0) -> httpx.Response:
        """Backoff-aware GET. Honors Retry-After on 429/503; falls back to
        exponential backoff with jitter on 429/5xx and transport errors.
        Returns the final Response — caller decides how to handle non-2xx.
        """
        assert self._client is not None, "Use as async context manager"
        last_exc: Exception | None = None
        for attempt in range(max_retries):
            try:
                r = await self._client.get(url, params=params, headers=headers)
            except httpx.TransportError as e:
                last_exc = e
                wait = min(max_backoff, (2 ** attempt) + random.random())
                logger.warning("transport error %s on %s, retrying in %.1fs",
                               e, url, wait)
                await asyncio.sleep(wait)
                continue

            if r.status_code == 429 or r.status_code >= 500:
                ra = _parse_retry_after(r.headers.get("Retry-After"))
                wait = ra if ra is not None else min(max_backoff, (2 ** attempt) + random.random())
                wait = min(wait, max_backoff)
                logger.warning("HTTP %s on %s, backing off %.1fs (Retry-After=%s, attempt=%d)",
                               r.status_code, url, wait, ra, attempt + 1)
                await asyncio.sleep(wait)
                continue
            return r
        if last_exc is not None:
            raise last_exc
        return r  # exhausted retries with non-2xx — caller handles

    @retry(
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=1, max=20),
        retry=retry_if_exception_type(_RETRYABLE),
        reraise=True,
    )
    async def get_text(self, url: str, params: dict | None = None) -> tuple[int, str]:
        """GET returning (status_code, text). 404s are returned (not raised)
        so adapters can use them as walk-end signals.
        """
        assert self._client is not None, "Use as async context manager"
        logger.debug("GET %s params=%s", url, params)
        r = await self._client.get(url, params=params)
        if r.status_code == 404:
            return (404, "")
        if r.status_code >= 500 or r.status_code == 429:
            r.raise_for_status()
        if r.status_code >= 400:
            r.raise_for_status()
        return (r.status_code, r.text)
