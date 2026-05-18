"""Lightweight visitor tracking — separate DuckDB file from main `conductor.duckdb`.

Why separate: the main DB is seed-managed (`SEED_DB_URL`) and gets re-seeded when
the URL bumps. Analytics writes belong somewhere that survives that operation.
DuckDB ATTACH still lets us cross-join if a future report wants to mix bills + views.

Privacy: IPs are hashed with a daily-rotating salt, so within a day we can count
unique visitors but across days the same IP produces a different hash. No PII
on disk.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import duckdb

logger = logging.getLogger(__name__)


def default_db_path() -> Path:
    override = os.environ.get("ANALYTICS_DB", "").strip()
    if override:
        return Path(override)
    main_db = os.environ.get("CONDUCTOR_DB", "/data/conductor.duckdb").strip()
    p = Path(main_db)
    return p.parent / "analytics.duckdb"


SCHEMA_SQL = """
CREATE SEQUENCE IF NOT EXISTS page_view_id_seq START 1;

CREATE TABLE IF NOT EXISTS page_view (
    id            BIGINT PRIMARY KEY DEFAULT nextval('page_view_id_seq'),
    ts            TIMESTAMPTZ NOT NULL,
    path          VARCHAR NOT NULL,
    status        SMALLINT NOT NULL,
    method        VARCHAR NOT NULL,
    referer_host  VARCHAR,
    ua            VARCHAR,
    is_bot        BOOLEAN NOT NULL DEFAULT FALSE,
    visitor_hash  VARCHAR,
    dur_ms        INTEGER
);

CREATE INDEX IF NOT EXISTS idx_page_view_ts          ON page_view (ts);
CREATE INDEX IF NOT EXISTS idx_page_view_path        ON page_view (path);
CREATE INDEX IF NOT EXISTS idx_page_view_visitor     ON page_view (visitor_hash);
CREATE INDEX IF NOT EXISTS idx_page_view_referer     ON page_view (referer_host);
"""

# Don't log assets, JSON APIs, infra. Pageviews only.
SKIP_PREFIXES = ("/static/", "/api/", "/photo/", "/docs", "/openapi.json")
SKIP_PATHS = {"/robots.txt", "/sitemap.xml", "/favicon.ico", "/stats"}

BOT_RE = re.compile(
    r"(bot|crawler|spider|wget|curl|python-requests|httpx|aiohttp|"
    r"googlebot|bingbot|baiduspider|yandex|facebookexternalhit|slackbot|"
    r"twitterbot|linkedinbot|whatsapp|telegram|discord|preview|monitor|"
    r"uptime|pingdom|datadog|newrelic|semrush|ahrefs|petalbot|applebot|"
    r"duckduckbot|mj12bot|dotbot|seekport|gptbot|claudebot|ccbot|"
    r"perplexitybot|amazonbot|bytespider|headlesschrome)",
    re.I,
)

# Path patterns hit only by automated vulnerability scanners. Tallyhq doesn't
# run WordPress / phpMyAdmin / Drupal / etc., so any request to these is a
# bot regardless of the User-Agent it claims. UAs commonly spoof Mozilla, so
# UA regex alone won't catch them — flag by path.
PROBE_PATH_RE = re.compile(
    r"^/("
    r"wp-(admin|login|content|includes|json)|xmlrpc\.php|"
    r"\.env|\.git|\.aws|\.ssh|\.svn|\.htaccess|\.DS_Store|"
    r"phpmyadmin|pma|myadmin|adminer|mysql|"
    r"administrator|joomla|drupal|magento|"
    r"vendor/phpunit|"
    r"cgi-bin|fcgi-bin|"
    r"actuator|server-status|server-info|"
    r"shell\.php|cmd\.php|c99\.php|r57\.php|webshell|"
    r"backup\.zip|backup\.tar|database\.sql|dump\.sql|"
    r"config\.php|configuration\.php|wp-config|"
    r"owa|exchange|autodiscover|ews/|"
    r"console|jenkins|kibana|elastic|"
    r"laravel|symfony|"
    r"_ignition|telescope/|horizon/|"
    r"setup|install\.php|installer"
    r")(/|\.|$)",
    re.I,
)


def should_skip(path: str) -> bool:
    if path in SKIP_PATHS:
        return True
    return any(path.startswith(p) for p in SKIP_PREFIXES)


def is_bot(ua: str | None) -> bool:
    if not ua:
        return True  # treat absent UA as bot
    return bool(BOT_RE.search(ua))


def is_probe_path(path: str) -> bool:
    """Path looks like a known vuln-scanner probe (WordPress install wizard,
    phpMyAdmin, .env exfil, etc.). Tallyhq runs none of these stacks, so any
    hit is automated regardless of claimed UA."""
    return bool(PROBE_PATH_RE.match(path or ""))


def _base_salt() -> str:
    # If unset, the salt is deterministic — fine, since we're hashing per-day
    # already; the env override just adds privacy if you don't trust the box.
    return os.environ.get("ANALYTICS_SALT", "tallyhq-default-salt-v1")


def _daily_salt(d: date | None = None) -> str:
    d = d or datetime.now(tz=timezone.utc).date()
    h = hashlib.sha256(f"{_base_salt()}|{d.isoformat()}".encode()).hexdigest()
    return h[:32]


def visitor_hash(ip: str | None, ua: str | None) -> str | None:
    if not ip:
        return None
    h = hashlib.sha256(f"{_daily_salt()}|{ip}|{ua or ''}".encode()).hexdigest()
    return h[:16]


def referer_host(referer: str | None, own_host: str) -> str | None:
    if not referer:
        return None
    try:
        host = (urlparse(referer).hostname or "").lower() or None
    except Exception:
        return None
    if host and host == own_host.lower():
        return None  # internal nav
    return host


def client_ip(request) -> str | None:
    """Extract real client IP. Railway puts the original at the front of XFF."""
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    return request.client.host if request.client else None


class AnalyticsStore:
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or default_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(str(self.db_path))
        # Sidecar DB — tiny INSERT-per-request workload. Cap aggressively so it
        # doesn't double the buffer-pool footprint alongside the main DB.
        # See knowledge/tools/duckdb-default-memory-limit-ignores-cgroups.md
        self.conn.execute("SET memory_limit = '128MB'")
        self.conn.execute("SET threads = 2")
        self.conn.execute("SET temp_directory = '/tmp/duckdb-analytics'")
        self.conn.execute(SCHEMA_SQL)

    def close(self):
        self.conn.close()

    def record(
        self,
        *,
        path: str,
        status: int,
        method: str,
        referer_host: str | None,
        ua: str | None,
        is_bot: bool,
        visitor_hash: str | None,
        dur_ms: int | None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO page_view
                (ts, path, status, method, referer_host, ua, is_bot, visitor_hash, dur_ms)
            VALUES (NOW(), ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                path[:2000],
                int(status),
                method[:8],
                referer_host[:255] if referer_host else None,
                (ua or "")[:500],
                bool(is_bot),
                visitor_hash,
                int(dur_ms) if dur_ms is not None else None,
            ],
        )

    def summary(self, *, days: int = 7, exclude_bots: bool = True) -> dict:
        """Aggregate stats for the stats page."""
        bot_clause = "AND NOT is_bot" if exclude_bots else ""

        totals = self.conn.execute(
            f"""
            SELECT COUNT(*), COUNT(DISTINCT visitor_hash)
            FROM page_view
            WHERE ts >= NOW() - INTERVAL '{int(days)} days' {bot_clause}
            """
        ).fetchone()

        today = self.conn.execute(
            f"""
            SELECT COUNT(*), COUNT(DISTINCT visitor_hash)
            FROM page_view
            WHERE ts >= NOW() - INTERVAL '1 day' {bot_clause}
            """
        ).fetchone()

        top_paths = self.conn.execute(
            f"""
            SELECT path, COUNT(*) AS n, COUNT(DISTINCT visitor_hash) AS uv
            FROM page_view
            WHERE ts >= NOW() - INTERVAL '{int(days)} days' {bot_clause}
            GROUP BY path
            ORDER BY n DESC
            LIMIT 25
            """
        ).fetchall()

        top_referers = self.conn.execute(
            f"""
            SELECT referer_host, COUNT(*) AS n
            FROM page_view
            WHERE ts >= NOW() - INTERVAL '{int(days)} days' {bot_clause}
              AND referer_host IS NOT NULL
            GROUP BY referer_host
            ORDER BY n DESC
            LIMIT 20
            """
        ).fetchall()

        by_day = self.conn.execute(
            f"""
            SELECT CAST(ts AT TIME ZONE 'UTC' AS DATE) AS d,
                   COUNT(*) AS pv,
                   COUNT(DISTINCT visitor_hash) AS uv
            FROM page_view
            WHERE ts >= NOW() - INTERVAL '{int(days)} days' {bot_clause}
            GROUP BY d
            ORDER BY d
            """
        ).fetchall()

        bot_split = self.conn.execute(
            f"""
            SELECT
                COUNT(*) FILTER (WHERE is_bot)     AS bots,
                COUNT(*) FILTER (WHERE NOT is_bot) AS humans
            FROM page_view
            WHERE ts >= NOW() - INTERVAL '{int(days)} days'
            """
        ).fetchone()

        top_bots = self.conn.execute(
            f"""
            SELECT
                CASE
                    -- Traditional search
                    WHEN ua ILIKE '%googlebot%'         THEN 'Googlebot'
                    WHEN ua ILIKE '%bingbot%'           THEN 'Bingbot'
                    WHEN ua ILIKE '%applebot%'          THEN 'Applebot'
                    WHEN ua ILIKE '%duckduckbot%'       THEN 'DuckDuckBot'
                    WHEN ua ILIKE '%yandex%'            THEN 'YandexBot'
                    -- LLM training crawlers
                    WHEN ua ILIKE '%gptbot%'            THEN 'GPTBot'
                    WHEN ua ILIKE '%claudebot%'         THEN 'ClaudeBot'
                    WHEN ua ILIKE '%ccbot%'             THEN 'CCBot'
                    WHEN ua ILIKE '%meta-externalagent%' THEN 'Meta-ExternalAgent'
                    WHEN ua ILIKE '%meta-webindexer%'   THEN 'Meta-WebIndexer'
                    -- LLM search / on-demand fetchers (drive human downstream traffic)
                    WHEN ua ILIKE '%oai-search%'        THEN 'OAI-SearchBot'
                    WHEN ua ILIKE '%chatgpt-user%'      THEN 'ChatGPT-User'
                    WHEN ua ILIKE '%perplexitybot%'     THEN 'PerplexityBot'
                    WHEN ua ILIKE '%xai-search%'        THEN 'xAI-SearchBot'
                    WHEN ua ILIKE '%amazonbot%'         THEN 'Amazonbot'
                    -- SEO / backlink commercial crawlers (zero value to us)
                    WHEN ua ILIKE '%semrush%'           THEN 'SemrushBot'
                    WHEN ua ILIKE '%ahrefs%'            THEN 'AhrefsBot'
                    WHEN ua ILIKE '%mj12bot%'           THEN 'MJ12bot'
                    WHEN ua ILIKE '%serpstatbot%'       THEN 'serpstatbot'
                    WHEN ua ILIKE '%bytespider%'        THEN 'Bytespider'
                    -- Social link unfurlers
                    WHEN ua ILIKE '%facebookexternalhit%' THEN 'Facebook'
                    WHEN ua ILIKE '%twitterbot%'        THEN 'Twitterbot'
                    WHEN ua ILIKE '%slackbot%'          THEN 'Slackbot'
                    WHEN ua ILIKE '%linkedinbot%'       THEN 'LinkedInBot'
                    WHEN ua ILIKE '%discordbot%'        THEN 'Discordbot'
                    -- Dev / scripted clients
                    WHEN ua ILIKE '%curl%'              THEN 'curl'
                    WHEN ua ILIKE '%python-requests%'   THEN 'python-requests'
                    WHEN ua ILIKE '%httpx%'             THEN 'httpx'
                    ELSE 'other-bot'
                END AS bot_name,
                COUNT(*) AS n
            FROM page_view
            WHERE ts >= NOW() - INTERVAL '{int(days)} days'
              AND is_bot
            GROUP BY bot_name
            ORDER BY n DESC
            LIMIT 20
            """
        ).fetchall()

        return {
            "days": int(days),
            "exclude_bots": exclude_bots,
            "pv_7d": int(totals[0] or 0),
            "uv_7d": int(totals[1] or 0),
            "pv_today": int(today[0] or 0),
            "uv_today": int(today[1] or 0),
            "top_paths": [{"path": r[0], "n": int(r[1]), "uv": int(r[2])} for r in top_paths],
            "top_referers": [{"host": r[0], "n": int(r[1])} for r in top_referers],
            "by_day": [{"day": str(r[0]), "pv": int(r[1]), "uv": int(r[2])} for r in by_day],
            "bot_pv": int(bot_split[0] or 0),
            "human_pv": int(bot_split[1] or 0),
            "top_bots": [{"name": r[0], "n": int(r[1])} for r in top_bots],
        }
