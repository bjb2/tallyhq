"""Shared async HTTP client with retry."""
from __future__ import annotations

import logging

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": "Conductor/0.1",
    "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
}

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
