"""govinfo bill-text adapter — DB-free.

Walks govinfo bulk JSON listings under
    https://www.govinfo.gov/bulkdata/json/BILLS/{congress}/{session}/{bill_type}

and fetches the Formatted Text (HTML) for any package not yet stored on disk.
Stores cleaned plain-text + per-bill manifest.json under data/bill_text/.

No DB reads, no DB writes — never contends for the DuckDB file lock. A
downstream aggregator can later join manifest.json files back to the bills
table at its leisure.

Cursor: per (congress, session, bill_type) JSON sidecar tracking the last
`lastModified` timestamp seen, so re-pulls skip already-walked listings.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator

import httpx

from conductor.adapters.base import Adapter, registry
from conductor.events import Event
from conductor.politics.bill_text import (
    DEFAULT_TEXT_ROOT,
    STAGE_LABELS,
    clean_html_to_lines,
    extract_version_code,
    read_manifest,
    text_path,
    upsert_manifest_entry,
    write_text,
)

logger = logging.getLogger(__name__)

BULK_JSON = "https://www.govinfo.gov/bulkdata/json/BILLS/{congress}/{session}/{btype}/"
PKG_HTML = "https://www.govinfo.gov/content/pkg/{pkg}/html/{pkg}.htm"
PKG_XML = "https://www.govinfo.gov/content/pkg/{pkg}/xml/{pkg}.xml"
LMT_FMT = "%d-%b-%Y %H:%M"  # govinfo formattedLastModifiedTime, e.g. "10-Jan-2025 20:36"

BILL_TYPES = ("hr", "hjres", "hconres", "hres", "s", "sjres", "sconres", "sres")
DEFAULT_CONGRESS = 119
DEFAULT_SESSIONS = (1, 2)

PER_FETCH_SLEEP = 0.3  # ~3 req/s — polite on govinfo's live HTML endpoint
# Cap on fetches per pull; float('inf') means uncapped. Cursor advances per
# package so a Ctrl-C mid-run loses at most one in-flight fetch.
MAX_FETCHES_PER_PULL: float = float("inf")

# Filename pattern: BILLS-{congress}{type}{number}{code}.xml
_PKG_NAME_RE = re.compile(
    r"^BILLS-(\d+)(hr|hjres|hconres|hres|s|sjres|sconres|sres)(\d+)([a-z0-9]+)\.(xml|htm|html|pdf)$",
    re.IGNORECASE,
)


@registry.register
class GovInfoBillTextAdapter(Adapter):
    name = "govinfo_bill_text"
    schema_version = 1
    requires_db = False  # filesystem-only — never touches the events DB

    @property
    def root(self) -> Path:
        return DEFAULT_TEXT_ROOT

    @property
    def cursor_path(self) -> Path:
        return self.root / ".cursors.json"

    def _load_cursors(self) -> dict:
        p = self.cursor_path
        if not p.exists():
            return {}
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _save_cursors(self, cursors: dict) -> None:
        self.cursor_path.parent.mkdir(parents=True, exist_ok=True)
        self.cursor_path.write_text(json.dumps(cursors, indent=2), encoding="utf-8")

    async def pull(self) -> AsyncIterator[Event]:
        client = self.http._client
        if client is None:
            raise RuntimeError("HttpClient not entered")

        cursors = self._load_cursors()
        congresses = _parse_congresses(cursors.get("_congresses"))
        fetched_total = 0
        budget = MAX_FETCHES_PER_PULL

        logger.info("[%s] starting — congresses=%s sessions=%s types=%d budget=%s root=%s",
                    self.name, congresses, list(DEFAULT_SESSIONS), len(BILL_TYPES),
                    "∞" if budget == float("inf") else int(budget), self.root)

        for congress in congresses:
            for session in DEFAULT_SESSIONS:
                for btype in BILL_TYPES:
                    if budget <= 0:
                        logger.info("[%s] budget exhausted, stopping early", self.name)
                        break
                    key = f"{congress}/{session}/{btype}"
                    last_seen = cursors.get(key, "")
                    logger.info("[%s] walking %s (cursor=%s, budget=%s)",
                                self.name, key, last_seen or "<cold>",
                                "∞" if budget == float("inf") else int(budget))
                    new_last_seen, fetched, budget = await self._walk(
                        client, congress, session, btype, last_seen, budget,
                    )
                    fetched_total += fetched
                    logger.info("[%s] %s done — fetched %d, total %d, cursor %s",
                                self.name, key, fetched, fetched_total, new_last_seen or "<unset>")
                    if new_last_seen and new_last_seen != last_seen:
                        cursors[key] = new_last_seen
                        self._save_cursors(cursors)

        logger.info("[%s] DONE — fetched %d new text versions", self.name, fetched_total)

        if False:
            yield  # pragma: no cover

    async def _walk(self, client: httpx.AsyncClient, congress: int, session: int,
                    btype: str, last_seen: str,
                    budget: float) -> tuple[str, int, float]:
        """Walk the bulk listing for (congress, session, btype). Returns
        (new_last_seen_iso, fetched_count, remaining_budget).
        """
        url = BULK_JSON.format(congress=congress, session=session, btype=btype)
        try:
            r = await self.http.get(url, headers={"Accept": "application/json"})
        except httpx.HTTPError as e:
            logger.warning("[%s] listing fetch failed %s: %s", self.name, url, e)
            return last_seen, 0, budget
        if r.status_code == 404:
            logger.info("[%s] listing 404 (no bills yet for this slot): %s", self.name, url)
            return last_seen, 0, budget
        if r.status_code >= 400:
            logger.warning("[%s] listing %s: HTTP %s", self.name, url, r.status_code)
            return last_seen, 0, budget

        try:
            data = r.json()
        except (json.JSONDecodeError, ValueError):
            logger.warning("[%s] listing %s: bad JSON", self.name, url)
            return last_seen, 0, budget

        files = data.get("files") or []
        # `files` lists one entry per format (.xml only currently). Dedupe by
        # package stem in case other formats appear later.
        packages: dict[str, dict] = {}
        for f in files:
            name = f.get("name") or ""
            m = _PKG_NAME_RE.match(name)
            if not m:
                continue
            stem = name.rsplit(".", 1)[0]
            lm_iso = _parse_lmt(f.get("formattedLastModifiedTime") or "")
            existing = packages.get(stem)
            if existing is None or lm_iso > (existing.get("lm_iso") or ""):
                packages[stem] = {"stem": stem, "match": m, "lm_iso": lm_iso}

        ordered = sorted(packages.values(), key=lambda p: p.get("lm_iso") or "")
        logger.info("[%s] %d/%d/%s — %d packages in listing",
                    self.name, congress, session, btype, len(ordered))

        new_last_seen = last_seen
        fetched = 0
        skipped = 0
        examined = 0
        for pkg in ordered:
            lm = pkg.get("lm_iso") or ""
            if last_seen and lm <= last_seen:
                continue
            if budget <= 0:
                break
            examined += 1
            stem = pkg["stem"]
            m = pkg["match"]
            cong, btype_real, num, code = (
                m.group(1), m.group(2).lower(), m.group(3), m.group(4).lower(),
            )

            have = read_manifest(self.root, cong, btype_real, num)
            if any(e.get("version_code") == code for e in have):
                skipped += 1
                if lm > new_last_seen:
                    new_last_seen = lm
                continue

            logger.info("[%s] fetch %s (lm=%s)", self.name, stem, lm)
            entry = await _fetch_package(client, stem, cong, btype_real, num, code,
                                         lm, self.root)
            if entry:
                upsert_manifest_entry(self.root, cong, btype_real, num, entry)
                fetched += 1
                budget -= 1
                logger.info("[%s]   stored %s.%s lines=%d sha=%s",
                            self.name, stem, code, entry["lines"], entry["sha256"][:12])
            else:
                logger.warning("[%s]   skipped %s (no usable text)", self.name, stem)
            if lm > new_last_seen:
                new_last_seen = lm
            await asyncio.sleep(PER_FETCH_SLEEP)

            if examined % 50 == 0:
                logger.info("[%s] progress %d/%d/%s — examined=%d fetched=%d skipped=%d budget=%s",
                            self.name, congress, session, btype,
                            examined, fetched, skipped,
                            "∞" if budget == float("inf") else int(budget))

        return new_last_seen, fetched, budget


def _parse_lmt(s: str) -> str:
    """Convert govinfo's `formattedLastModifiedTime` ("10-Jan-2025 20:36") to
    a sortable ISO string. Returns "" on parse failure.
    """
    if not s:
        return ""
    try:
        dt = datetime.strptime(s, LMT_FMT).replace(tzinfo=timezone.utc)
        return dt.isoformat()
    except ValueError:
        return ""


def _parse_congresses(raw) -> list[int]:
    """Allow CLI/env override later; default to current Congress."""
    if isinstance(raw, list) and all(isinstance(x, int) for x in raw):
        return raw
    return [DEFAULT_CONGRESS]


async def _fetch_package(client: httpx.AsyncClient, stem: str, congress, btype,
                         number, code: str, lm: str, root: Path) -> dict | None:
    """Fetch HTML (preferred) or XML for a single package, clean, persist."""
    candidates = [
        PKG_HTML.format(pkg=stem),
        PKG_XML.format(pkg=stem),
    ]
    raw = None
    used_url = None
    for url in candidates:
        try:
            r = await client.get(url)
        except httpx.HTTPError as e:
            logger.info("transport %s: %s", url, e)
            continue
        if r.status_code == 404:
            continue
        if r.status_code >= 400:
            logger.info("%s %s", r.status_code, url)
            continue
        raw = r.text
        used_url = url
        break
    if raw is None:
        return None
    lines = clean_html_to_lines(raw)
    if not lines:
        logger.warning("empty text after clean: %s", stem)
        return None
    path = text_path(root, congress, btype, number, code)
    sha = write_text(path, lines)
    return {
        "version_code": code,
        "version_label": STAGE_LABELS.get(code, code.upper()),
        "package": stem,
        "path": str(path).replace("\\", "/"),
        "fetched_at": datetime.now(tz=timezone.utc).isoformat(),
        "source_url": used_url,
        "last_modified": lm,
        "sha256": sha,
        "lines": len(lines),
    }
