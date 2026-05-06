"""Cook Partisan Voting Index sync — Wikipedia mirror.

Cook Political Report's official spreadsheet is paywalled. The Wikipedia
article "Cook Partisan Voting Index" carries the full per-district and
per-state tables for the current Congress, sourced from Cook. We treat
Wikipedia as the canonical free mirror until/unless we acquire a CPR
subscription.

Updates rarely (per-Congress, occasional mid-cycle revisions) so we
re-pull on the same Monday weekly cadence as legislators / committees.

Schema:
  district_pvi (
    congress, state, district (NULL for Senate / state-level), pvi,
    score (signed: + = R lean, - = D lean, 0 = EVEN), source, updated_at
  )
"""
from __future__ import annotations

import logging
import re

import httpx
from bs4 import BeautifulSoup

from conductor.store import Store

logger = logging.getLogger(__name__)

WIKI_URL = "https://en.wikipedia.org/wiki/Cook_Partisan_Voting_Index"

# Default Congress for at-rest rows. The Wikipedia table reflects the
# current Congress; for older bills/legislators we'd need historical
# tables (out of scope for v1).
CURRENT_CONGRESS = 119

PVI_SCHEMA_SQL = """
-- district = -1 indicates a state-level (Senate) row. Sentinel rather
-- than NULL because DuckDB's PRIMARY KEY rejects NULL.
CREATE TABLE IF NOT EXISTS district_pvi (
    congress    INTEGER NOT NULL,
    state       VARCHAR NOT NULL,
    district    INTEGER NOT NULL,
    pvi         VARCHAR NOT NULL,
    score       INTEGER NOT NULL,
    source      VARCHAR NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (congress, state, district)
);

CREATE INDEX IF NOT EXISTS idx_pvi_state_dist ON district_pvi (state, district);
"""

STATE_LEVEL = -1


# Full state name → USPS code. Includes DC for completeness even though
# DC has no congressional district with voting representation.
STATE_NAME_TO_CODE = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN", "Mississippi": "MS",
    "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY",
    "North Carolina": "NC", "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK",
    "Oregon": "OR", "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY", "District of Columbia": "DC",
    "Washington, D.C.": "DC",   # Wikipedia's CPVI state table spelling
}


def ensure_schema(store: Store) -> None:
    store.conn.execute(PVI_SCHEMA_SQL)


def parse_pvi(s: str) -> tuple[str, int] | None:
    """Normalize a PVI cell like 'R+5', 'D+12', 'EVEN' to (pvi_str, score).

    score is signed: positive = Republican lean, negative = Democratic lean,
    0 = EVEN. Returns None on unparseable input.
    """
    if s is None:
        return None
    t = re.sub(r"\s+", "", s.upper())
    # Strip parenthetical commentary like 'R+5(x)' some Wikipedia rows carry
    t = re.sub(r"\(.*?\)", "", t)
    if t in ("EVEN", "EV", "—", "-", ""):
        return ("EVEN", 0)
    m = re.match(r"^([RD])\+(\d+)$", t)
    if not m:
        return None
    sign = 1 if m.group(1) == "R" else -1
    return (f"{m.group(1)}+{m.group(2)}", sign * int(m.group(2)))


def parse_district(s: str) -> int | None:
    """'At-large' → 0, '12' → 12, '12th' → 12. None on garbage."""
    t = (s or "").strip()
    if not t or t.lower() in ("at-large", "at large", "al"):
        return 0
    m = re.match(r"^(\d+)", t)
    return int(m.group(1)) if m else None


def parse_combined_district_cell(s: str) -> tuple[str, int] | None:
    """Wikipedia's CPVI 'By congressional district' table merges state + seat
    into one cell — e.g. 'Alabama\xa01', 'Wyoming\xa0at-large'. Split into
    (state_code, district)."""
    if not s:
        return None
    # Normalize NBSP and similar whitespace
    t = re.sub(r"\s+", " ", s.replace("\xa0", " ")).strip()
    # Walk longest-prefix match against known state names
    for name in sorted(STATE_NAME_TO_CODE, key=len, reverse=True):
        if t.startswith(name + " "):
            tail = t[len(name) + 1:].strip()
            d = parse_district(tail)
            if d is not None:
                return (STATE_NAME_TO_CODE[name], d)
            return None
        # Some redistricted entries omit the trailing space, e.g. just
        # 'Wyomingat-large' if Wikipedia loses its NBSP. Tolerate it.
        if t.startswith(name) and len(t) > len(name):
            tail = t[len(name):].strip()
            d = parse_district(tail)
            if d is not None:
                return (STATE_NAME_TO_CODE[name], d)
    return None


def _state_code(text: str) -> str | None:
    """Match the leading state name in a cell. Wikipedia state cells often
    contain a Wikipedia link followed by other text; we strip down to the
    longest leading state-name match."""
    if not text:
        return None
    t = text.strip()
    # Try longest-match first to handle 'New' / 'North' / 'South' / 'West' prefixes
    for name in sorted(STATE_NAME_TO_CODE, key=len, reverse=True):
        if t.startswith(name):
            return STATE_NAME_TO_CODE[name]
    # Last-ditch: USPS code if someone tabulates it directly
    if len(t) == 2 and t.upper() in STATE_NAME_TO_CODE.values():
        return t.upper()
    return None


def _header_index(headers: list[str]) -> dict[str, int]:
    """Map normalized column names to header indices."""
    idx: dict[str, int] = {}
    for i, h in enumerate(headers):
        h = h.strip().lower()
        if "pvi" in h:
            idx.setdefault("pvi", i)
        elif h in ("state", "states"):
            idx.setdefault("state", i)
        elif h in ("district", "cd", "congressional district"):
            idx.setdefault("district", i)
    return idx


def _cell_text(td) -> str:
    return td.get_text(separator=" ", strip=True)


def _parse_table(table) -> tuple[str, list[dict]]:
    """Return (table_kind, rows). table_kind is 'district', 'state', or 'unknown'.

    Three table shapes recognized:
      A. state-only:    headers contain 'state' + 'pvi'         → state-level rows
      B. split district: headers contain 'state' + 'district' + 'pvi' → district rows
      C. merged district: headers contain 'district' + 'pvi' (no 'state'),
         district cell is 'Alabama 1' / 'Wyoming at-large' → district rows
    """
    head = table.find("thead")
    if head:
        header_cells = head.find_all(["th", "td"])
    else:
        first_row = table.find("tr")
        header_cells = first_row.find_all(["th", "td"]) if first_row else []
    headers = [_cell_text(c) for c in header_cells]
    idx = _header_index(headers)
    if "pvi" not in idx:
        return ("unknown", [])
    has_state = "state" in idx
    has_district = "district" in idx
    if not (has_state or has_district):
        return ("unknown", [])
    kind = "district" if has_district else "state"

    rows: list[dict] = []
    body = table.find("tbody") or table
    for tr in body.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        if len(cells) <= max(idx.values()):
            continue
        # Header row inside tbody (Wikipedia sometimes re-emits it)
        if all(c.name == "th" for c in cells):
            continue
        pvi_text = _cell_text(cells[idx["pvi"]])
        parsed = parse_pvi(pvi_text)
        if parsed is None:
            continue
        pvi_str, score = parsed

        state: str | None = None
        district: int | None = None
        if has_state and has_district:
            state = _state_code(_cell_text(cells[idx["state"]]))
            district = parse_district(_cell_text(cells[idx["district"]]))
        elif has_district and not has_state:
            sd = parse_combined_district_cell(_cell_text(cells[idx["district"]]))
            if sd:
                state, district = sd
        else:  # state-only
            state = _state_code(_cell_text(cells[idx["state"]]))

        if not state:
            continue
        if has_district and district is None:
            continue

        rows.append({
            "state": state,
            "district": district,  # None for state table
            "pvi": pvi_str,
            "score": score,
        })
    return (kind, rows)


def fetch_html(url: str = WIKI_URL, timeout: float = 30.0) -> str:
    headers = {"User-Agent": "tallyhq/0.2 (+https://tallyhq.org) httpx"}
    resp = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=True)
    resp.raise_for_status()
    return resp.text


def parse_pvi_html(html: str) -> tuple[list[dict], list[dict]]:
    """Return (district_rows, state_rows). Each row: {state, district?, pvi, score}."""
    soup = BeautifulSoup(html, "html.parser")
    district_rows: list[dict] = []
    state_rows: list[dict] = []
    for table in soup.find_all("table"):
        cls = " ".join(table.get("class") or [])
        if "wikitable" not in cls:
            continue
        kind, rows = _parse_table(table)
        if kind == "district" and len(rows) > len(district_rows):
            district_rows = rows
        elif kind == "state" and len(rows) > len(state_rows):
            state_rows = rows
    return district_rows, state_rows


def sync(store: Store, congress: int = CURRENT_CONGRESS) -> int:
    """Pull the Wikipedia tables and upsert into district_pvi.

    Returns total rows written (district + state).
    """
    ensure_schema(store)
    html = fetch_html()
    district_rows, state_rows = parse_pvi_html(html)
    logger.info("pvi: parsed %d district rows, %d state rows",
                len(district_rows), len(state_rows))

    # Replace this congress wholesale to avoid stale rows from prior
    # district maps lingering after a redistricting.
    store.conn.execute("DELETE FROM district_pvi WHERE congress = ?", [congress])

    written = 0
    for r in district_rows:
        store.conn.execute(
            """
            INSERT INTO district_pvi
                (congress, state, district, pvi, score, source, updated_at)
            VALUES (?, ?, ?, ?, ?, 'wikipedia', NOW())
            """,
            [congress, r["state"], r["district"], r["pvi"], r["score"]],
        )
        written += 1
    for r in state_rows:
        # district = STATE_LEVEL (-1) sentinel for state-level / Senate rows
        store.conn.execute(
            """
            INSERT INTO district_pvi
                (congress, state, district, pvi, score, source, updated_at)
            VALUES (?, ?, ?, ?, ?, 'wikipedia', NOW())
            """,
            [congress, r["state"], STATE_LEVEL, r["pvi"], r["score"]],
        )
        written += 1
    logger.info("pvi: wrote %d total rows for congress %d", written, congress)
    return written


def bulk_for_members(
    store: Store,
    members: list[tuple[str, int | None, str]],   # list of (state, district, chamber)
    congress: int = CURRENT_CONGRESS,
) -> dict[tuple[str, int, str], dict]:
    """Resolve PVI for many legislators in one query. Returns a dict keyed
    by (state, district_or_0, chamber_lower)."""
    if not members:
        return {}
    ensure_schema(store)
    rows = store.conn.execute(
        "SELECT state, district, pvi, score, source FROM district_pvi WHERE congress = ?",
        [congress],
    ).fetchall()
    by_key: dict[tuple[str, int], dict] = {}
    for state, district, pvi, score, source in rows:
        by_key[(state, int(district))] = {
            "pvi": pvi, "score": int(score), "source": source,
        }
    out: dict[tuple[str, int, str], dict] = {}
    for state, district, chamber in members:
        chamber_low = (chamber or "").lower()
        if chamber_low == "house":
            d = int(district) if district is not None else 0
        else:
            d = STATE_LEVEL
        rec = by_key.get((state, d))
        if rec is not None:
            out[(state, int(district) if district is not None else 0, chamber_low)] = rec
    return out


def for_member(
    store: Store,
    state: str,
    district: int | None,
    chamber: str,
    congress: int = CURRENT_CONGRESS,
) -> dict | None:
    """Lookup PVI for a legislator. House: match state + district. Senate
    or non-voting: state-level row (district IS NULL)."""
    ensure_schema(store)
    if chamber.lower() == "house":
        rows = store.conn.execute(
            """
            SELECT pvi, score, source FROM district_pvi
            WHERE congress = ? AND state = ? AND district = ?
            """,
            [congress, state, district if district is not None else 0],
        ).fetchall()
    else:
        rows = store.conn.execute(
            """
            SELECT pvi, score, source FROM district_pvi
            WHERE congress = ? AND state = ? AND district = ?
            """,
            [congress, state, STATE_LEVEL],
        ).fetchall()
    if not rows:
        return None
    pvi, score, source = rows[0]
    return {"pvi": pvi, "score": int(score), "source": source}
