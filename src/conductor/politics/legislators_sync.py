"""Sync the legislators table from @unitedstates/congress-legislators YAML.

Source-of-truth: github.com/unitedstates/congress-legislators (community-maintained,
authoritative crosswalk of bioguide/govtrack/fec/opensecrets/icpsr IDs).

Pulled file: legislators-current.yaml (currently-serving members).
"""
from __future__ import annotations

import logging
from datetime import date

import httpx
import yaml

from conductor.politics.entities import FederalEntity, upsert_legislators
from conductor.store import Store

logger = logging.getLogger(__name__)

LEGISLATORS_URL = (
    "https://unitedstates.github.io/congress-legislators/legislators-current.yaml"
)


def _to_entity(rec: dict) -> FederalEntity | None:
    name = rec.get("name") or {}
    ids = rec.get("id") or {}
    bioguide = ids.get("bioguide")
    if not bioguide:
        return None

    terms = rec.get("terms") or []
    if not terms:
        return None
    last = terms[-1]
    chamber = "house" if last.get("type") == "rep" else "senate"
    served_from_str = last.get("start")
    served_until_str = last.get("end")
    try:
        served_from = date.fromisoformat(served_from_str) if served_from_str else None
        served_until = date.fromisoformat(served_until_str) if served_until_str else None
    except ValueError:
        return None

    return FederalEntity(
        bioguide_id=bioguide,
        first_name=name.get("first") or "",
        last_name=name.get("last") or "",
        full_name=name.get("official_full") or f"{name.get('first', '')} {name.get('last', '')}".strip(),
        chamber=chamber,
        state=last.get("state") or "",
        district=last.get("district"),
        party=last.get("party") or "",
        served_from=served_from or date(1900, 1, 1),
        served_until=served_until,
        ids={
            "govtrack": ids.get("govtrack"),
            "fec": ids.get("fec"),
            "opensecrets": ids.get("opensecrets"),
            "icpsr": ids.get("icpsr"),
            "thomas": ids.get("thomas"),
            "lis": ids.get("lis"),                # Senate vote XML uses this
            "votesmart": ids.get("votesmart"),
            "wikipedia": ids.get("wikipedia"),
        },
    )


def fetch_yaml(url: str = LEGISLATORS_URL) -> list[dict]:
    logger.info("fetching legislators from %s", url)
    r = httpx.get(url, timeout=60.0, follow_redirects=True)
    r.raise_for_status()
    return yaml.safe_load(r.text)


def sync(store: Store, url: str = LEGISLATORS_URL) -> int:
    raw = fetch_yaml(url)
    entities = [e for e in (_to_entity(r) for r in raw) if e is not None]
    n = upsert_legislators(store, entities)
    logger.info("upserted %d legislators", n)
    return n
