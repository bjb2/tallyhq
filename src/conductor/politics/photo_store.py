"""Photo store — own the bytes, commit to repo, drop 3rd-party runtime calls.

Storage layout:
    {repo}/src/conductor/politics/web/static/photos/{bioguide_id}.jpg

Web serves these via FastAPI's StaticFiles mount at /static/photos/. No
runtime DB, no /data volume, no per-request 3rd-party fetches. Photos
are tracked in git, deterministic across deploys, edge-cached for free.

Refresh flow (developer machine):
    python -m conductor.cli politics sync-photos
    git add src/conductor/politics/web/static/photos
    git commit -m "photos: refresh 119th Congress portraits"
    git push

Source resolution chain (per member, in order, first hit wins):
    1. unitedstates/images @ gh-pages /225x275/{bioguide}.jpg
       (Community-maintained mirror; covers ~83% of current members.)
    2. api.congress.gov  member.depiction.imageUrl
       (Authoritative when CONGRESS_GOV_API_KEY is set; used for the ~17%
        the unitedstates mirror lags on, mostly current-Congress freshmen.)
    3. Wikipedia REST  /api/rest_v1/page/summary/{slug}  -> originalimage
       (Free, no key; covers the long tail when api.congress.gov has no
        depiction populated yet. Most freshman portraits land here.)

We do not 302-redirect to remote URLs anymore. That pattern caused the
"all photos disappeared overnight" incident — congress.gov rotated
image URL hashes and the DB still held stale pointers.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import httpx

from conductor.secrets import get as get_secret

logger = logging.getLogger(__name__)


def default_root() -> Path:
    """Where photo files live.

    Default: `src/conductor/politics/web/static/photos/` — committed to the
    repo and served by FastAPI's StaticFiles mount at /static/photos. This
    keeps photos fully reproducible across deploys with no volume
    dependency. The CLI `politics sync-photos` writes here at dev time;
    refresh = commit + push.

    Override via PHOTO_STORE_DIR env for local experimentation.
    """
    override = os.environ.get("PHOTO_STORE_DIR", "").strip()
    if override:
        return Path(override)
    return Path(__file__).resolve().parent / "web" / "static" / "photos"


def photo_path(root: Path, bioguide_id: str) -> Path:
    return root / f"{bioguide_id}.jpg"


# Refresh files older than this on the next sync_all run.
REFRESH_AFTER = timedelta(days=30)
# Cap downloaded file size to guard against runaway responses.
MAX_BYTES = 5 * 1024 * 1024  # 5 MB
# Per-host pacing — be polite to api.congress.gov and raw.githubusercontent.com.
PER_FETCH_SLEEP = 0.05


UNITEDSTATES_BASE = "https://raw.githubusercontent.com/unitedstates/images/gh-pages/congress"
API_CONGRESS = "https://api.congress.gov/v3"
WIKIPEDIA_REST = "https://en.wikipedia.org/api/rest_v1/page/summary"
WIKIPEDIA_UA = "tallyhq/1.0 (https://tallyhq.org)"


@dataclass
class SyncResult:
    bioguide: str
    source: str  # 'congress_gov' | 'unitedstates' | 'cached' | 'missing' | 'error'
    bytes_written: int = 0
    error: str = ""


def _is_fresh(p: Path) -> bool:
    if not p.exists():
        return False
    mtime = datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc)
    return (datetime.now(tz=timezone.utc) - mtime) < REFRESH_AFTER


async def _fetch_image(client: httpx.AsyncClient, url: str) -> Optional[bytes]:
    """GET an image, retrying on 429/5xx. Returns raw bytes (any image
    format) or None. Caller is responsible for transcoding to the
    canonical JPEG output.
    """
    for attempt in range(3):
        try:
            r = await client.get(url, timeout=15.0, follow_redirects=True)
        except httpx.HTTPError as e:
            logger.debug("fetch %s: %s", url, e)
            await asyncio.sleep(1.0 * (attempt + 1))
            continue
        if r.status_code in (429, 502, 503, 504):
            ra = r.headers.get("retry-after")
            wait = float(ra) if (ra and ra.isdigit()) else (1.0 * (attempt + 1))
            await asyncio.sleep(min(wait, 5.0))
            continue
        if r.status_code != 200:
            return None
        break
    else:
        return None
    ctype = (r.headers.get("content-type") or "").lower()
    if not (ctype.startswith("image/") or "octet-stream" in ctype):
        logger.debug("fetch %s: non-image content-type %r", url, ctype)
        return None
    body = r.content
    if not body or len(body) > MAX_BYTES:
        return None
    return body


async def _resolve_via_api(
    client: httpx.AsyncClient, bioguide: str, api_key: str
) -> Optional[str]:
    try:
        r = await client.get(
            f"{API_CONGRESS}/member/{bioguide}",
            params={"format": "json"},
            headers={"X-Api-Key": api_key},
            timeout=10.0,
        )
    except httpx.HTTPError as e:
        logger.debug("api.congress.gov fetch %s: %s", bioguide, e)
        return None
    if r.status_code != 200:
        return None
    try:
        d = r.json()
    except ValueError:
        return None
    return ((d.get("member") or {}).get("depiction") or {}).get("imageUrl")


async def _resolve_wikipedia_candidates(
    client: httpx.AsyncClient, slug: str
) -> list[str]:
    """Return ordered candidate image URLs from a Wikipedia page summary:
    [originalimage, thumbnail]. The transcoder tries each in turn so we
    fall back to the auto-PNG-transcoded thumbnail when the originalimage
    is something Pillow can't open (very rare) or fails to download.
    Retries on 429/5xx.
    """
    if not slug:
        return []
    import urllib.parse as _up
    encoded = _up.quote(slug.replace(" ", "_"))
    url = f"{WIKIPEDIA_REST}/{encoded}"
    for attempt in range(3):
        try:
            r = await client.get(url, timeout=10.0)
        except httpx.HTTPError as e:
            logger.debug("wikipedia fetch %s: %s", slug, e)
            await asyncio.sleep(1.0 * (attempt + 1))
            continue
        if r.status_code in (429, 502, 503, 504):
            ra = r.headers.get("retry-after")
            wait = float(ra) if (ra and ra.isdigit()) else (1.0 * (attempt + 1))
            await asyncio.sleep(min(wait, 5.0))
            continue
        if r.status_code != 200:
            return []
        try:
            d = r.json()
        except ValueError:
            return []
        out: list[str] = []
        orig = ((d.get("originalimage") or {}).get("source") or "")
        thumb = ((d.get("thumbnail") or {}).get("source") or "")
        if orig:
            out.append(orig)
        if thumb and thumb != orig:
            out.append(thumb)
        return out
    return []


def _transcode_to_jpeg(body: bytes) -> Optional[bytes]:
    """Decode any Pillow-supported format and re-encode as JPEG.

    Wikimedia serves WebP for many freshman portraits; congress.gov serves
    JPEG; unitedstates serves JPEG. Re-encoding to one canonical format
    means the on-disk file extension and HTTP Content-Type both stay
    `image/jpeg`, browser-compatible everywhere, and the repo doesn't
    accumulate format variants per refresh.

    Strips alpha + EXIF + ICC; resizes to fit within 600x800 to bound
    storage. Returns JPEG bytes or None on decode failure.
    """
    import io
    try:
        from PIL import Image, ImageOps
    except ImportError:
        logger.warning("PIL not available; skipping transcode")
        return body
    try:
        img = Image.open(io.BytesIO(body))
        img = ImageOps.exif_transpose(img) or img
        if img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")
        img.thumbnail((600, 800), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85, optimize=True, progressive=True)
        return buf.getvalue()
    except Exception as e:
        logger.warning("transcode failed: %s", e)
        return None


def _atomic_write(target: Path, body: bytes) -> int:
    jpeg = _transcode_to_jpeg(body)
    if jpeg is None:
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".jpg.partial")
    tmp.write_bytes(jpeg)
    os.replace(tmp, target)
    return len(jpeg)


async def sync_one(
    client: httpx.AsyncClient, bioguide: str, root: Path, *,
    api_key: str = "", wikipedia_slug: str = "", force: bool = False,
) -> SyncResult:
    """Download (or refresh) the photo for a single member. Idempotent."""
    if not bioguide or not bioguide[0].isalpha():
        return SyncResult(bioguide, "missing", error="invalid bioguide")
    target = photo_path(root, bioguide)
    if not force and _is_fresh(target):
        return SyncResult(bioguide, "cached")

    # 1) community mirror — unitedstates/images 225x275 (cheapest, most coverage)
    us_url = f"{UNITEDSTATES_BASE}/225x275/{bioguide}.jpg"
    body = await _fetch_image(client, us_url)
    if body:
        n = _atomic_write(target, body)
        if n:
            return SyncResult(bioguide, "unitedstates", bytes_written=n)

    # 2) api.congress.gov member.depiction (authoritative, requires key)
    if api_key:
        url = await _resolve_via_api(client, bioguide, api_key)
        if url:
            body = await _fetch_image(client, url)
            if body:
                n = _atomic_write(target, body)
                if n:
                    return SyncResult(bioguide, "congress_gov", bytes_written=n)

    # 3) Wikipedia REST summary -> originalimage / thumbnail
    if wikipedia_slug:
        for wp_url in await _resolve_wikipedia_candidates(client, wikipedia_slug):
            body = await _fetch_image(client, wp_url)
            if body:
                n = _atomic_write(target, body)
                if n:
                    return SyncResult(bioguide, "wikipedia", bytes_written=n)

    return SyncResult(bioguide, "missing")


async def sync_all(
    members: list[tuple[str, str]], root: Path, *,
    force: bool = False, concurrency: int = 3,
) -> dict[str, int]:
    """Download photos for every member.

    `members` is `[(bioguide_id, wikipedia_slug), ...]`. Pass `""` for
    members without a Wikipedia slug.

    Returns per-source counts. Bounded concurrency keeps Wikipedia + GitHub
    raw happy. Wikipedia rate-limits aggressively under burst load; serial
    pacing per request is required, hence concurrency=6.
    """
    api_key = get_secret("CONGRESS_GOV_API_KEY") or ""
    counts: dict[str, int] = {
        "unitedstates": 0, "congress_gov": 0, "wikipedia": 0,
        "cached": 0, "missing": 0, "error": 0,
    }
    missing: list[str] = []
    sem = asyncio.Semaphore(concurrency)
    root.mkdir(parents=True, exist_ok=True)

    async def _one(client: httpx.AsyncClient, bg: str, slug: str) -> None:
        async with sem:
            try:
                res = await sync_one(client, bg, root, api_key=api_key,
                                     wikipedia_slug=slug, force=force)
                counts[res.source] = counts.get(res.source, 0) + 1
                if res.source in ("unitedstates", "congress_gov", "wikipedia"):
                    logger.info("photo %s: %s (%d bytes)", bg, res.source, res.bytes_written)
                elif res.source == "missing":
                    missing.append(bg)
                    logger.warning("photo %s: NO SOURCE", bg)
            except Exception as e:
                counts["error"] = counts.get("error", 0) + 1
                logger.error("photo %s: ERROR %s", bg, e)
            await asyncio.sleep(PER_FETCH_SLEEP)

    # Wikimedia (upload.wikimedia.org) requires a User-Agent — without one
    # both the REST summary endpoint and the Commons image CDN return 403.
    # Setting it at the client level covers both calls per request.
    async with httpx.AsyncClient(
        follow_redirects=True,
        headers={"User-Agent": WIKIPEDIA_UA},
    ) as client:
        await asyncio.gather(*(_one(client, bg, slug) for bg, slug in members))
    if missing:
        counts["_missing_bioguides"] = missing  # type: ignore[assignment]
    return counts


def have(root: Path, bioguide: str) -> bool:
    return photo_path(root, bioguide).exists()
