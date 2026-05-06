"""Bill text storage, normalization, diff helpers, and DB ingest.

Two-stage pipeline:
  1. govinfo_bill_text adapter writes plain-text + manifest.json to disk under
     data/bill_text/{congress}/{bill_type}/{number}/. FS-only — never touches
     the DuckDB file lock during the long pull.
  2. ingest_from_fs() walks that tree and loads bodies into a `bill_text`
     table during a known-idle DB window. The table is the source of truth
     for prod (ships in the .duckdb release artifact); the FS tree is local
     intermediate state and is gitignored.

Web layer reads from the DB (changelog_for_bill / load_diff). Diffs are
rendered on demand and cached in-process (lru_cache).
"""
from __future__ import annotations

import difflib
import functools
import hashlib
import html
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from conductor.store import Store

logger = logging.getLogger(__name__)

# Override with TALLYHQ_TEXT_ROOT (or BILL_TEXT_ROOT) for cross-cwd setups,
# e.g. running tallyhq's web from a different dir than where data/ lives.
DEFAULT_TEXT_ROOT = Path(
    os.environ.get("TALLYHQ_TEXT_ROOT")
    or os.environ.get("BILL_TEXT_ROOT")
    or "data/bill_text"
)

# Stage ordering — earlier in the legislative process first.
STAGE_ORDER = [
    "ih", "is",       # Introduced
    "rh", "rs",       # Reported
    "rfh", "rfs",     # Referred
    "cph", "cps",     # Considered + Passed
    "eh", "es",       # Engrossed
    "eah", "eas",     # Engrossed Amendment
    "rds", "rdh",     # Received in opposite chamber
    "pcs",            # Placed on Calendar Senate
    "ats",            # Agreed to Senate
    "ath",            # Agreed to House
    "enr",            # Enrolled
    "pl",             # Public Law
    "pp",             # Public Print
    "es2",
]

STAGE_LABELS = {
    "ih": "Introduced (House)", "is": "Introduced (Senate)",
    "rh": "Reported (House)",   "rs": "Reported (Senate)",
    "rfh": "Referred (House)",  "rfs": "Referred (Senate)",
    "rfs2": "Referred (Senate, revised)",
    "rhuc": "Reported (House, unanimous consent)",
    "cph": "Considered & Passed (House)", "cps": "Considered & Passed (Senate)",
    "eh": "Engrossed (House)",  "es": "Engrossed (Senate)",
    "eh1s": "Engrossed (House) Amendment (Senate)",
    "eah": "Engrossed Amendment (House)", "eas": "Engrossed Amendment (Senate)",
    "rds": "Received (Senate)", "rdh": "Received (House)",
    "pcs": "Placed on Calendar (Senate)",
    "pch": "Placed on Calendar (House)",
    "ats": "Agreed to (Senate)", "ath": "Agreed to (House)",
    "enr": "Enrolled",
    "pl": "Public Law", "pp": "Public Print",
    "es2": "Engrossed (Senate, revised)",
    "lth": "Laid on Table (House)",
    "lts": "Laid on Table (Senate)",
}


# ---------------------------------------------------------------------------
# DB schema + ingest + reads
# ---------------------------------------------------------------------------

BILL_TEXT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bill_text (
    bill_id        VARCHAR,
    version_code   VARCHAR,
    version_label  VARCHAR,
    body           VARCHAR,
    line_count     INTEGER,
    sha256         VARCHAR,
    package        VARCHAR,
    source_url     VARCHAR,
    last_modified  TIMESTAMPTZ,
    fetched_at     TIMESTAMPTZ,
    PRIMARY KEY (bill_id, version_code)
);
CREATE INDEX IF NOT EXISTS idx_bill_text_bill ON bill_text(bill_id);
"""


def ensure_schema(store: "Store") -> None:
    if getattr(store, "read_only", False):
        return
    store.conn.execute(BILL_TEXT_SCHEMA_SQL)


def _resolve_label(code: str, stored: str | None) -> str:
    """STAGE_LABELS first, then a real stored label, then upper-cased code."""
    canonical = STAGE_LABELS.get(code)
    if canonical:
        return canonical
    if stored and stored != code:
        return stored
    return code.upper()


def _parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def ingest_from_fs(store: "Store", root: Path = DEFAULT_TEXT_ROOT,
                   *, replace: bool = False) -> tuple[int, int]:
    """Walk ``root`` for manifests + .txt and load into the bill_text table.

    Returns (inserted, skipped). When ``replace`` is False, rows already
    present (matched by sha256) are skipped — re-ingest is a cheap no-op.
    """
    ensure_schema(store)
    inserted = 0
    skipped = 0
    if not root.exists():
        logger.warning("ingest: root does not exist: %s", root)
        return 0, 0

    for manifest in root.rglob("manifest.json"):
        try:
            entries = json.loads(manifest.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("ingest: bad manifest %s: %s", manifest, e)
            continue
        if not isinstance(entries, list):
            continue

        bill_dir = manifest.parent
        # path: root/{congress}/{bill_type}/{number}/manifest.json
        try:
            number = bill_dir.name
            bill_type = bill_dir.parent.name
            congress = bill_dir.parent.parent.name
        except (IndexError, AttributeError):
            continue
        bill_id = f"{congress}:{bill_type}:{number}"

        for entry in entries:
            code = entry.get("version_code")
            if not code:
                continue
            sha = entry.get("sha256") or ""
            if not replace and sha:
                existing = store.conn.execute(
                    "SELECT sha256 FROM bill_text WHERE bill_id = ? AND version_code = ?",
                    [bill_id, code],
                ).fetchone()
                if existing and existing[0] == sha:
                    skipped += 1
                    continue

            txt_path = bill_dir / f"{code}.txt"
            if not txt_path.exists():
                logger.warning("ingest: missing text file %s", txt_path)
                continue
            body = txt_path.read_text(encoding="utf-8")

            store.conn.execute(
                """
                INSERT INTO bill_text (bill_id, version_code, version_label, body,
                                        line_count, sha256, package, source_url,
                                        last_modified, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (bill_id, version_code) DO UPDATE SET
                    version_label = excluded.version_label,
                    body          = excluded.body,
                    line_count    = excluded.line_count,
                    sha256        = excluded.sha256,
                    package       = excluded.package,
                    source_url    = excluded.source_url,
                    last_modified = excluded.last_modified,
                    fetched_at    = excluded.fetched_at
                """,
                [
                    bill_id, code,
                    _resolve_label(code, entry.get("version_label")),
                    body,
                    int(entry.get("lines") or 0),
                    sha,
                    entry.get("package") or "",
                    entry.get("source_url") or "",
                    _parse_iso(entry.get("last_modified")),
                    _parse_iso(entry.get("fetched_at")) or datetime.now(tz=timezone.utc),
                ],
            )
            inserted += 1
    logger.info("ingest_from_fs: inserted=%d skipped=%d root=%s", inserted, skipped, root)
    return inserted, skipped


def get_versions_db(store: "Store", bill_id: str) -> list[dict]:
    ensure_schema(store)
    rows = store.conn.execute(
        """
        SELECT version_code, version_label, line_count, sha256, fetched_at
        FROM bill_text
        WHERE bill_id = ?
        """,
        [bill_id],
    ).fetchall()
    out = []
    for code, label, lines, sha, fetched in rows:
        out.append({
            "version_code": code,
            "version_label": label,
            "lines": int(lines or 0),
            "sha256": sha or "",
            "fetched_at": fetched.isoformat() if fetched else None,
        })
    return out


def get_body_db(store: "Store", bill_id: str, version_code: str) -> Optional[str]:
    ensure_schema(store)
    row = store.conn.execute(
        "SELECT body FROM bill_text WHERE bill_id = ? AND version_code = ?",
        [bill_id, version_code],
    ).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Storage (filesystem — adapter output, ingest input)
# ---------------------------------------------------------------------------

MANIFEST_NAME = "manifest.json"


def manifest_path(root: Path, congress, bill_type, number) -> Path:
    return text_dir(root, congress, bill_type, number) / MANIFEST_NAME


def read_manifest(root: Path, congress, bill_type, number) -> list[dict]:
    p = manifest_path(root, congress, bill_type, number)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except json.JSONDecodeError:
        return []


def write_manifest(root: Path, congress, bill_type, number,
                   entries: list[dict]) -> None:
    p = manifest_path(root, congress, bill_type, number)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(entries, indent=2, default=str), encoding="utf-8")


def upsert_manifest_entry(root: Path, congress, bill_type, number,
                          entry: dict) -> None:
    existing = [e for e in read_manifest(root, congress, bill_type, number)
                if e.get("version_code") != entry.get("version_code")]
    existing.append(entry)
    existing.sort(key=lambda e: stage_sort_key(e.get("version_code", "")))
    write_manifest(root, congress, bill_type, number, existing)


# ---------------------------------------------------------------------------
# HTML cleaning
# ---------------------------------------------------------------------------

_BLOCK_TAGS = {"p", "div", "section", "article", "br", "li", "tr", "h1",
               "h2", "h3", "h4", "h5", "h6", "pre", "blockquote"}
_SKIP_TAGS = {"script", "style", "meta", "link", "head"}


class _TextExtractor(HTMLParser):
    """Pull plain text from govinfo bill HTML preserving paragraph breaks."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_endtag(self, tag):
        if tag in _SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
        elif tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth:
            return
        self._parts.append(data)

    def text(self) -> str:
        return "".join(self._parts)


# Page furniture in govinfo bill HTML that produces noisy diffs.
_NOISE_PATTERNS = [
    re.compile(r"^\s*\d+\s*$"),                # bare line numbers
    re.compile(r"^\s*Page\s+\d+\s*$", re.I),   # page footers
    re.compile(r"^\s*\[?\s*Congressional Bills.*\]?\s*$", re.I),
    re.compile(r"^\s*\[?H(R|J|CON|RES)\s+\d+.*\]?\s*$", re.I),  # bill banners
]


def clean_html_to_lines(raw_html: str) -> list[str]:
    """Strip markup, normalize whitespace, drop page furniture. Returns lines."""
    if not raw_html:
        return []
    parser = _TextExtractor()
    try:
        parser.feed(raw_html)
        parser.close()
    except Exception as e:
        logger.warning("HTML parse failed, falling back to regex: %s", e)
        text = re.sub(r"<[^>]+>", " ", raw_html)
        text = html.unescape(text)
    else:
        text = parser.text()

    lines: list[str] = []
    for raw_line in text.splitlines():
        # Collapse internal whitespace; trim
        line = re.sub(r"[ \t]+", " ", raw_line).strip()
        if not line:
            # Preserve a single blank line between paragraphs
            if lines and lines[-1] != "":
                lines.append("")
            continue
        if any(p.match(line) for p in _NOISE_PATTERNS):
            continue
        lines.append(line)
    # Drop trailing blanks
    while lines and lines[-1] == "":
        lines.pop()
    return lines


# ---------------------------------------------------------------------------
# Filesystem layout
# ---------------------------------------------------------------------------

def text_dir(root: Path, congress: int | str, bill_type: str, number: int | str) -> Path:
    return root / str(congress) / str(bill_type) / str(number)


def text_path(root: Path, congress: int | str, bill_type: str,
              number: int | str, version_code: str) -> Path:
    return text_dir(root, congress, bill_type, number) / f"{version_code}.txt"


def diff_path(root: Path, congress: int | str, bill_type: str,
              number: int | str, from_code: str, to_code: str) -> Path:
    return text_dir(root, congress, bill_type, number) / "diffs" / f"{from_code}-{to_code}.html"


def write_text(path: Path, lines: list[str]) -> str:
    """Write lines to disk, return sha256 hex of body."""
    body = "\n".join(lines) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8", newline="\n")
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def read_text(path: Path) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(encoding="utf-8").splitlines()


# ---------------------------------------------------------------------------
# Version-code extraction from govinfo URLs
# ---------------------------------------------------------------------------

# Pattern: BILLS-{congress}{type}{number}{version_code}
# e.g. BILLS-119hr1234ih, BILLS-119s55enr
_BILLS_PKG_RE = re.compile(
    r"BILLS-(\d+)(hr|hjres|hconres|hres|s|sjres|sconres|sres)(\d+)([a-z0-9]+)",
    re.IGNORECASE,
)


def extract_version_code(url: str) -> Optional[str]:
    if not url:
        return None
    m = _BILLS_PKG_RE.search(url)
    return m.group(4).lower() if m else None


def stage_sort_key(code: str) -> tuple[int, str]:
    try:
        return (STAGE_ORDER.index(code), code)
    except ValueError:
        return (999, code)


# ---------------------------------------------------------------------------
# Diff rendering
# ---------------------------------------------------------------------------

@dataclass
class DiffStats:
    added: int = 0
    removed: int = 0
    unchanged: int = 0


def render_unified_diff(from_lines: list[str], to_lines: list[str],
                        from_label: str, to_label: str) -> tuple[str, DiffStats]:
    """Return (html, stats). Inline GitHub-style: `+`/`-` lines tinted."""
    stats = DiffStats()
    out: list[str] = ['<div class="diff-body">']
    diff = difflib.unified_diff(
        from_lines, to_lines,
        fromfile=from_label, tofile=to_label,
        lineterm="",
        n=3,
    )
    for line in diff:
        esc = html.escape(line)
        if line.startswith("+++") or line.startswith("---"):
            out.append(f'<div class="diff-file">{esc}</div>')
        elif line.startswith("@@"):
            out.append(f'<div class="diff-hunk">{esc}</div>')
        elif line.startswith("+"):
            stats.added += 1
            out.append(f'<div class="diff-add">{esc}</div>')
        elif line.startswith("-"):
            stats.removed += 1
            out.append(f'<div class="diff-del">{esc}</div>')
        else:
            stats.unchanged += 1
            out.append(f'<div class="diff-ctx">{esc}</div>')
    out.append("</div>")

    # Empty-diff sentinel
    if stats.added == 0 and stats.removed == 0 and stats.unchanged == 0:
        return ('<div class="diff-empty">No differences detected.</div>', stats)
    return ("\n".join(out), stats)


def cached_render(root: Path, congress: int | str, bill_type: str,
                  number: int | str, from_code: str, to_code: str,
                  from_lines: list[str], to_lines: list[str]) -> tuple[str, DiffStats]:
    """Render-and-cache. Cache invalidated by recomputing whenever caller deletes file."""
    cache = diff_path(root, congress, bill_type, number, from_code, to_code)
    if cache.exists():
        try:
            data = json.loads(cache.read_text(encoding="utf-8"))
            return data["html"], DiffStats(**data["stats"])
        except (json.JSONDecodeError, KeyError, TypeError):
            pass  # fall through and re-render

    html_str, stats = render_unified_diff(
        from_lines, to_lines,
        STAGE_LABELS.get(from_code, from_code),
        STAGE_LABELS.get(to_code, to_code),
    )
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(
        json.dumps({"html": html_str, "stats": stats.__dict__}),
        encoding="utf-8",
    )
    return html_str, stats


# ---------------------------------------------------------------------------
# Convenience for the web layer
# ---------------------------------------------------------------------------

def changelog_for_bill(store: "Store", bill_id: str) -> dict:
    """Assemble Changelog payload from the bill_text table."""
    parts = bill_id.split(":")
    if len(parts) != 3:
        return {"versions": [], "default_from": None, "default_to": None}
    files = get_versions_db(store, bill_id)
    files.sort(key=lambda e: stage_sort_key(e.get("version_code", "")))
    versions = []
    for e in files:
        code = e.get("version_code", "")
        # STAGE_LABELS is the canonical translation. Fall through to a stored
        # version_label only when it's a real human string (not the code echo
        # the early adapter wrote), then to the code itself as last resort.
        label = STAGE_LABELS.get(code)
        if not label:
            stored = e.get("version_label") or ""
            label = stored if stored and stored != code else code.upper()
        versions.append({
            "code": code,
            "label": label,
            "path": e.get("path"),
            "fetched_at": e.get("fetched_at"),
            "sha256": e.get("sha256"),
            "lines": e.get("lines", 0),
        })
    default_from = versions[-2]["code"] if len(versions) >= 2 else None
    default_to = versions[-1]["code"] if versions else None
    return {
        "versions": versions,
        "default_from": default_from,
        "default_to": default_to,
    }


def load_diff(store: "Store", bill_id: str, from_code: str,
              to_code: str) -> Optional[tuple[str, DiffStats]]:
    """Load bodies from bill_text table and render diff. None if either missing."""
    from_body = get_body_db(store, bill_id, from_code)
    to_body = get_body_db(store, bill_id, to_code)
    if from_body is None or to_body is None:
        return None
    from_lines = from_body.splitlines()
    to_lines = to_body.splitlines()
    return _render_cached(bill_id, from_code, to_code,
                          tuple(from_lines), tuple(to_lines))


@functools.lru_cache(maxsize=512)
def _render_cached(bill_id: str, from_code: str, to_code: str,
                   from_lines: tuple[str, ...],
                   to_lines: tuple[str, ...]) -> tuple[str, DiffStats]:
    """In-process diff cache. Keyed by (bill_id, codes, content tuples) so
    cache invalidates automatically when text changes (different lines tuple).
    """
    return render_unified_diff(
        list(from_lines), list(to_lines),
        STAGE_LABELS.get(from_code, from_code),
        STAGE_LABELS.get(to_code, to_code),
    )
