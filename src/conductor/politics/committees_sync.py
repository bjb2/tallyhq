"""Committee assignments sync — entity state, not events.

Uses @unitedstates/congress-legislators YAML files (community-maintained,
authoritative). The api.congress.gov /member endpoint does NOT return
committee assignments; we'd otherwise have to walk /committee/{...}/members
for every committee, which is brittle and rate-limited.

Inputs:
  - committees-current.yaml         metadata (name, chamber, type, parent)
  - committee-membership-current.yaml  per-committee member lists with bioguide
"""
from __future__ import annotations

import logging

import httpx
import yaml

from conductor.store import Store

logger = logging.getLogger(__name__)

COMMITTEES_URL = "https://unitedstates.github.io/congress-legislators/committees-current.yaml"
MEMBERSHIP_URL = "https://unitedstates.github.io/congress-legislators/committee-membership-current.yaml"

COMMITTEES_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS committees (
    committee_code  VARCHAR PRIMARY KEY,
    name            VARCHAR,
    chamber         VARCHAR,            -- 'house' | 'senate' | 'joint'
    committee_type  VARCHAR,            -- 'standing', 'select', 'joint', etc.
    parent_code     VARCHAR,            -- non-null for subcommittees
    url             VARCHAR,
    updated_at      TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS committee_assignments (
    bioguide_id     VARCHAR NOT NULL,
    committee_code  VARCHAR NOT NULL,
    party           VARCHAR,            -- 'majority' | 'minority'
    rank_in_party   INTEGER,
    title           VARCHAR,            -- 'Chair', 'Ranking Member', or null
    updated_at      TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (bioguide_id, committee_code)
);

CREATE INDEX IF NOT EXISTS idx_ca_committee ON committee_assignments (committee_code);
CREATE INDEX IF NOT EXISTS idx_committees_parent ON committees (parent_code);
CREATE INDEX IF NOT EXISTS idx_committees_chamber ON committees (chamber);
"""


def ensure_schema(store: Store) -> None:
    store.conn.execute(COMMITTEES_SCHEMA_SQL)


def _flatten_committees(committees: list[dict]) -> list[dict]:
    """The committees YAML nests subcommittees under their parent. Flatten."""
    out: list[dict] = []
    for c in committees:
        out.append({
            "code": c.get("thomas_id") or c.get("house_committee_id") or c.get("senate_committee_id"),
            "name": c.get("name") or "",
            "chamber": (c.get("type") or "").lower() or "joint",
            "committee_type": "standing",
            "parent_code": None,
            "url": c.get("url") or c.get("address") or "",
        })
        for sc in (c.get("subcommittees") or []):
            parent_code = c.get("thomas_id") or c.get("house_committee_id") or c.get("senate_committee_id")
            sc_code = (parent_code or "") + (sc.get("thomas_id") or "")
            out.append({
                "code": sc_code,
                "name": sc.get("name") or "",
                "chamber": (c.get("type") or "").lower() or "joint",
                "committee_type": "subcommittee",
                "parent_code": parent_code,
                "url": sc.get("url") or "",
            })
    return [c for c in out if c["code"]]


def fetch_yaml(url: str) -> object:
    logger.info("fetching %s", url)
    r = httpx.get(url, timeout=60.0, follow_redirects=True)
    r.raise_for_status()
    return yaml.safe_load(r.text)


def sync(store: Store, **_unused) -> int:
    """Synchronous version — yaml fetch is fast, no concurrency needed."""
    ensure_schema(store)

    committees_data = fetch_yaml(COMMITTEES_URL)
    membership_data = fetch_yaml(MEMBERSHIP_URL)

    flat = _flatten_committees(committees_data or [])
    written_committees = 0
    for c in flat:
        store.conn.execute(
            """
            INSERT INTO committees
                (committee_code, name, chamber, committee_type, parent_code, url, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, NOW())
            ON CONFLICT (committee_code) DO UPDATE SET
                name = excluded.name,
                chamber = excluded.chamber,
                committee_type = excluded.committee_type,
                parent_code = excluded.parent_code,
                url = excluded.url,
                updated_at = NOW()
            """,
            [c["code"], c["name"], c["chamber"], c["committee_type"],
             c["parent_code"], c["url"]],
        )
        written_committees += 1

    # Wipe old assignments and re-insert (memberships change rarely; full rewrite is cheap)
    store.conn.execute("DELETE FROM committee_assignments")
    written = 0
    for code, members in (membership_data or {}).items():
        if not isinstance(members, list):
            continue
        for m in members:
            bg = m.get("bioguide")
            if not bg:
                continue
            store.conn.execute(
                """
                INSERT INTO committee_assignments
                    (bioguide_id, committee_code, party, rank_in_party, title, updated_at)
                VALUES (?, ?, ?, ?, ?, NOW())
                ON CONFLICT (bioguide_id, committee_code) DO UPDATE SET
                    party = excluded.party,
                    rank_in_party = excluded.rank_in_party,
                    title = excluded.title,
                    updated_at = NOW()
                """,
                [bg, code, m.get("party"), m.get("rank"), m.get("title")],
            )
            written += 1
    logger.info("committees: %d definitions, %d assignments", written_committees, written)
    return written


def member_committees(store: Store, bioguide: str) -> list[dict]:
    ensure_schema(store)
    rows = store.conn.execute(
        """
        SELECT a.committee_code, c.name, c.chamber, c.committee_type, c.parent_code,
               a.party, a.rank_in_party, a.title
        FROM committee_assignments a
        LEFT JOIN committees c ON c.committee_code = a.committee_code
        WHERE a.bioguide_id = ?
        ORDER BY c.committee_type, c.name
        """,
        [bioguide],
    ).fetchall()
    return [
        {
            "code": r[0], "name": r[1] or r[0], "chamber": r[2],
            "type": r[3], "parent_code": r[4],
            "party": r[5], "rank": r[6], "title": r[7],
        }
        for r in rows
    ]


def on_committee(store: Store, committee_code: str) -> list[str]:
    ensure_schema(store)
    rows = store.conn.execute(
        "SELECT bioguide_id FROM committee_assignments WHERE committee_code = ?",
        [committee_code],
    ).fetchall()
    return [r[0] for r in rows]
