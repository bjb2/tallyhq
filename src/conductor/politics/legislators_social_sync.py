"""Sync social-media handles for sitting legislators from
`@unitedstates/congress-legislators` (`legislators-social-media.yaml`).

Source-of-truth: github.com/unitedstates/congress-legislators (community-
maintained, daily PRs, ~95%+ coverage of sitting members). Fields per record:

    social:
      twitter:       primary public handle (campaign/personal usually)
      twitter_id:    numeric account ID (survives handle rename)
      youtube:       channel name
      youtube_id:    UC… channel ID
      instagram:     handle
      instagram_id:  numeric ID
      facebook:      page slug

We dump the whole YAML into a single `legislator_social` table, primary key
`bioguide_id`, replace-on-change. Cheap (~535 rows, <100KB).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
import yaml

from conductor.store import Store

logger = logging.getLogger(__name__)

SOCIAL_URL = (
    "https://unitedstates.github.io/congress-legislators/legislators-social-media.yaml"
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS legislator_social (
    bioguide_id   VARCHAR PRIMARY KEY,
    twitter       VARCHAR,
    twitter_id    BIGINT,
    youtube       VARCHAR,
    youtube_id    VARCHAR,
    instagram     VARCHAR,
    instagram_id  BIGINT,
    facebook      VARCHAR,
    updated_at    TIMESTAMPTZ NOT NULL
);
"""


def ensure_schema(store: Store) -> None:
    store.conn.execute(SCHEMA_SQL)


def fetch_yaml(url: str = SOCIAL_URL) -> list[dict]:
    logger.info("fetching legislators-social-media from %s", url)
    r = httpx.get(url, timeout=60.0, follow_redirects=True)
    r.raise_for_status()
    return yaml.safe_load(r.text) or []


def _row(rec: dict) -> tuple | None:
    ids = rec.get("id") or {}
    bg = ids.get("bioguide")
    if not bg:
        return None
    s = rec.get("social") or {}

    def _int(v):
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    return (
        bg,
        s.get("twitter") or None,
        _int(s.get("twitter_id")),
        s.get("youtube") or None,
        s.get("youtube_id") or None,
        s.get("instagram") or None,
        _int(s.get("instagram_id")),
        s.get("facebook") or None,
    )


def sync(store: Store, url: str = SOCIAL_URL) -> int:
    """Fetch the YAML, upsert all rows. Returns number of rows written."""
    ensure_schema(store)
    raw = fetch_yaml(url)
    rows = [r for r in (_row(rec) for rec in raw) if r is not None]
    now = datetime.now(tz=timezone.utc)
    written = 0
    for r in rows:
        store.conn.execute(
            """
            INSERT INTO legislator_social
                (bioguide_id, twitter, twitter_id, youtube, youtube_id,
                 instagram, instagram_id, facebook, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (bioguide_id) DO UPDATE SET
                twitter      = excluded.twitter,
                twitter_id   = excluded.twitter_id,
                youtube      = excluded.youtube,
                youtube_id   = excluded.youtube_id,
                instagram    = excluded.instagram,
                instagram_id = excluded.instagram_id,
                facebook     = excluded.facebook,
                updated_at   = excluded.updated_at
            """,
            [*r, now],
        )
        written += 1
    logger.info("upserted %d legislator_social rows", written)
    return written


def get(store: Store, bioguide_id: str) -> dict | None:
    """Fetch one row as a dict for template rendering. Returns None if no
    record OR if every handle is null (treat empty social as 'no row')."""
    try:
        row = store.conn.execute(
            """
            SELECT twitter, twitter_id, youtube, youtube_id,
                   instagram, instagram_id, facebook
            FROM legislator_social
            WHERE bioguide_id = ?
            """,
            [bioguide_id],
        ).fetchone()
    except Exception:
        return None  # table not yet created
    if row is None:
        return None
    twitter, twitter_id, youtube, youtube_id, instagram, instagram_id, facebook = row
    if not any([twitter, youtube, youtube_id, instagram, facebook]):
        return None
    return {
        "twitter": twitter,
        "twitter_id": twitter_id,
        "youtube": youtube,
        "youtube_id": youtube_id,
        "instagram": instagram,
        "instagram_id": instagram_id,
        "facebook": facebook,
    }
