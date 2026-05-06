"""FastAPI app — "GitHub for legislators".

Routes:
  GET /                          roster index, filterable by chamber/state/party
  GET /legislator/{bioguide}     profile page with activity-grid SVG + recent events
  GET /legislator/{bioguide}.svg standalone SVG (embeddable)
  GET /api/legislator/{bioguide} JSON dump of grid + recent events
"""
from __future__ import annotations

import os
import time
from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from conductor.aggregations.activity_grid import grid
from conductor.politics import (
    analytics as analytics_mod,
    bill_summary as bill_summary_mod,
    bill_text as bill_text_mod, bill_views, bills as bills_mod,
    committees_sync as committees_mod,
    entities, funding as funding_mod, landing as landing_mod,
    legislators_social_sync as social_mod,
    lobby_views, photos as photos_mod, pvi_sync as pvi_mod,
    rollcall_views, stats as stats_mod,
)
from conductor.politics.photos import photo_url
from conductor.politics.render_grid import render_svg
from conductor.store import Store

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"


def _env() -> Environment:
    import json as _json
    from conductor.politics.bills import parse_legis_num
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    def _from_json(v):
        if isinstance(v, str):
            try:
                return _json.loads(v)
            except Exception:
                return {}
        return v or {}
    def _bill_path(congress, legis_num):
        if not (congress and legis_num):
            return None
        parsed = parse_legis_num(legis_num)
        if not parsed:
            return None
        return f"/bill/{congress}/{parsed[0]}/{parsed[1]}"
    def _basic_md(text: str) -> str:
        """Tiny markdown renderer for AI summaries. Handles paragraphs,
        **bold**, *italic*, and `code`. Escapes other HTML."""
        import html as _html
        import re as _re
        if not text:
            return ""
        # Escape HTML first
        s = _html.escape(text)
        # Bold/italic/code (after escape so input HTML can't sneak through)
        s = _re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = _re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<em>\1</em>", s)
        s = _re.sub(r"`(.+?)`", r"<code>\1</code>", s)
        # Paragraphs by double newline; preserve single newlines as <br>
        paras = [p.strip() for p in s.split("\n\n") if p.strip()]
        return "\n".join(f"<p>{p.replace(chr(10), '<br>')}</p>" for p in paras)

    env.filters["from_json"] = _from_json
    env.filters["basic_md"] = _basic_md
    env.globals["bill_path"] = _bill_path
    env.globals["stage_for_action"] = _stage_for_action
    return env


def _fetch_bill_actions(store: Store, bill_id: str) -> list[dict]:
    """Pull bill.action events for a bill and dedupe.

    Two collapse passes:
      (1) Same action recorded by multiple sources (api vs bulk) — same
          (date, normalized_text). Pick first; merge committees lists.
      (2) congress.gov records one action-row per referring committee for
          multi-committee referrals — same (date, text) with different
          committees field. Merge committees into a single list.

    Result: one row per logical action, newest first, with deduped committee tags.
    """
    import json as _json
    rows = store.conn.execute(
        """
        SELECT occurred_at, source, payload
        FROM events
        WHERE event_type = 'bill.action'
          AND entity_id = ?
        ORDER BY occurred_at DESC, id DESC
        """,
        [f"bill:{bill_id}"],
    ).fetchall()

    grouped: dict[tuple, dict] = {}
    order: list[tuple] = []
    for occ, source, payload in rows:
        p = _json.loads(payload) if isinstance(payload, str) else (payload or {})
        text = (p.get("text") or "").strip()
        date_str = (p.get("action_date") or "")
        key = (date_str, text)
        if key not in grouped:
            grouped[key] = {"when": occ, "sources": [source], **p}
            grouped[key]["committees"] = list(p.get("committees") or [])
            order.append(key)
        else:
            existing = grouped[key]
            if source not in existing["sources"]:
                existing["sources"].append(source)
            for c in (p.get("committees") or []):
                if c not in existing["committees"]:
                    existing["committees"].append(c)
    return [grouped[k] for k in order]


def _is_passed(store: Store, bill_id: str) -> bool:
    """Bill has reached enrolled/law (text version), or latest_action says it
    became law / was signed. Used to gate AI summary auto-generation.
    """
    try:
        row = store.conn.execute(
            """
            SELECT 1
            FROM bill_text
            WHERE bill_id = ? AND version_code IN ('enr', 'pl')
            LIMIT 1
            """,
            [bill_id],
        ).fetchone()
        if row:
            return True
    except Exception:
        pass
    row = store.conn.execute(
        """
        SELECT 1 FROM bills
        WHERE bill_id = ?
          AND (LOWER(latest_action_text) LIKE '%became public law%'
               OR LOWER(latest_action_text) LIKE '%signed by president%')
        LIMIT 1
        """,
        [bill_id],
    ).fetchone()
    return bool(row)


def _fetch_crs_summaries(store: Store, bill_id: str) -> list[dict]:
    """Pull CRS summaries for a bill, newest first per version_code.

    Schema is created by the congress_bill_summaries adapter; if it hasn't
    run yet the table won't exist — handle gracefully.
    """
    try:
        rows = store.conn.execute(
            """
            SELECT version_code, action_desc, action_date, summary_text, update_date
            FROM bill_summaries
            WHERE bill_id = ?
            ORDER BY action_date DESC, update_date DESC
            """,
            [bill_id],
        ).fetchall()
    except Exception:
        return []
    seen: set[str] = set()
    out: list[dict] = []
    for code, desc, action_date, text, _ in rows:
        # One per version_code — newest action_date wins
        if code in seen:
            continue
        seen.add(code)
        out.append({
            "version_code": code,
            "action_desc": desc or "",
            "action_date": action_date,
            "summary_text": text or "",
        })
    return out


def _format_text_versions(versions: list[dict] | None) -> list[dict]:
    """Sort newest-first + add a human-readable `date_label`.

    govinfo emits dates like "2025-07-01T04:00:00Z" (ISO). We render
    "Jul 01, 2025" for display; missing dates sort to the end (so
    Enrolled Bill / Public Law without dates aren't artificially #1).
    """
    if not versions:
        return []
    from datetime import datetime as _dt

    def _parse(d: str | None):
        if not d:
            return None
        try:
            return _dt.fromisoformat(str(d).replace("Z", "+00:00"))
        except ValueError:
            return None

    # Map URL extension → format label. BILLSTATUS XML's `type` field is
    # almost always empty, so we infer from the URL.
    _EXT_LABEL = {
        "htm": "HTML", "html": "HTML",
        "pdf": "PDF",
        "xml": "XML",
        "txt": "TXT",
    }

    def _label_for(fmt: dict) -> str:
        t = (fmt.get("type") or "").strip()
        if t:
            return t
        url = fmt.get("url") or ""
        if not url:
            return ""
        ext = url.rsplit(".", 1)[-1].lower() if "." in url else ""
        return _EXT_LABEL.get(ext, ext.upper() or "FILE")

    out = []
    for v in versions:
        parsed = _parse(v.get("date"))
        formats = []
        for f in (v.get("formats") or []):
            label = _label_for(f)
            formats.append({**f, "label": label})
        out.append({
            **v,
            "formats": formats,
            "_sort": parsed,
            "date_label": parsed.strftime("%b %d, %Y") if parsed else "—",
        })
    # Newest first; None dates last
    out.sort(key=lambda x: (x["_sort"] is None, -(x["_sort"].timestamp() if x["_sort"] else 0)))
    return out


def _stage_for_action(action: dict) -> str:
    """Classify an action into one of: introduced, committee, floor, passed,
    sent, signed, vetoed, other. Used by the timeline to color stage bands.
    """
    code = (action.get("action_code") or "").upper()
    text = (action.get("text") or "").lower()
    atype = (action.get("action_type") or "").lower()
    if "intro" in atype or code in ("INTROH", "INTROS", "1000"):
        return "introduced"
    if "becamelaw" in atype or "becamepubliclaw" in atype or "becameprivatelaw" in atype \
            or "signed by president" in text:
        return "signed"
    if "vetoed" in text or "veto" in atype:
        return "vetoed"
    if "presented to president" in text or "to president" in text:
        return "sent"
    if "passed" in text or "agreed to" in text or "passed" in atype:
        return "passed"
    if "committee" in atype or "referred" in text or "reported" in text or "markup" in text:
        return "committee"
    if "floor" in atype or "consideration by" in text or "rule" in text:
        return "floor"
    return "other"


def _congress_term_context() -> dict:
    """Compute the term-progress context for the landing page.

    Each Congress lasts 2 years from Jan 3 of an odd year to Jan 3 of the next
    odd year. 119th = Jan 3 2025 → Jan 3 2027.
    """
    today = date.today()
    # Compute current Congress number from year (rough): 119 covers 2025-26
    if today.year >= 2025 and today.year < 2027:
        congress_num = 119
        start = date(2025, 1, 3)
        end = date(2027, 1, 3)
    elif today.year >= 2027 and today.year < 2029:
        congress_num = 120
        start = date(2027, 1, 3)
        end = date(2029, 1, 3)
    else:
        # Fallback for pre-2025 (shouldn't happen in production)
        congress_num = 118
        start = date(2023, 1, 3)
        end = date(2025, 1, 3)

    total_days = (end - start).days
    elapsed = max(0, min(total_days, (today - start).days))
    pct = round(100 * elapsed / total_days, 1)
    session = 1 if today.year == start.year else 2

    # Crude in-session vs recess: Congress is "in session" mostly Tue-Thu of
    # non-recess weeks. Without an authoritative calendar source we just say
    # "Day N of M" without claiming session/recess.
    return {
        "congress_num": congress_num,
        "session": session,
        "start": start,
        "end": end,
        "elapsed_days": elapsed,
        "total_days": total_days,
        "percent": pct,
    }


def _committee_options(store: Store) -> list[dict]:
    """Flat list of committees + subcommittees for the browse filter dropdown.
    Subcommittees are sorted under their parent so the <select> options
    render hierarchically with indentation."""
    try:
        rows = store.conn.execute(
            """
            SELECT committee_code, name, chamber, committee_type, parent_code
            FROM committees
            ORDER BY chamber, committee_type DESC, name
            """
        ).fetchall()
    except Exception:
        return []
    out: list[dict] = []
    primaries = [r for r in rows if r[3] != "subcommittee"]
    subs = [r for r in rows if r[3] == "subcommittee"]
    by_parent: dict[str, list] = {}
    for s in subs:
        by_parent.setdefault(s[4] or "", []).append(s)
    for p in primaries:
        out.append({"code": p[0], "name": p[1], "chamber": p[2], "is_sub": False})
        for s in by_parent.get(p[0], []):
            out.append({"code": s[0], "name": s[1], "chamber": s[2], "is_sub": True})
    return out


def _aggregate_stats(store: Store, bioguide_ids: list[str]) -> dict:
    """Aggregates over the events table for the supplied roster.
    Caller passes the (already-filtered) bioguides; we sum vote/break/bill/speech
    counts and surface top-3-most-active for that selection.
    """
    if not bioguide_ids:
        return {
            "members": 0, "by_party": {}, "votes": 0, "breaks": 0,
            "sponsored": 0, "cosponsored": 0, "speeches": 0, "top": [],
        }
    entity_ids = [f"bioguide:{bg}" for bg in bioguide_ids]
    placeholders = ",".join(["?"] * len(entity_ids))

    party_rows = store.conn.execute(
        f"SELECT party, COUNT(*) FROM legislators WHERE bioguide_id IN ({','.join(['?'] * len(bioguide_ids))}) GROUP BY 1",
        bioguide_ids,
    ).fetchall()
    by_party = {p or "—": int(n) for p, n in party_rows}

    counts = store.conn.execute(
        f"""
        SELECT
            SUM(CASE WHEN event_type = 'vote.cast' THEN 1 ELSE 0 END) AS votes,
            SUM(CASE WHEN event_type = 'vote.cast' AND json_extract_string(payload, '$.party_line') = 'false' THEN 1 ELSE 0 END) AS breaks,
            SUM(CASE WHEN event_type = 'bill.sponsored' THEN 1 ELSE 0 END) AS sponsored,
            SUM(CASE WHEN event_type = 'bill.cosponsored' THEN 1 ELSE 0 END) AS cosponsored,
            SUM(CASE WHEN event_type = 'floor.speech' THEN 1 ELSE 0 END) AS speeches
        FROM events
        WHERE entity_id IN ({placeholders})
        """,
        entity_ids,
    ).fetchone()
    votes, breaks, sponsored, cosponsored, speeches = (int(v or 0) for v in counts)

    top_rows = store.conn.execute(
        f"""
        SELECT entity_id, COUNT(*) AS n
        FROM events
        WHERE entity_id IN ({placeholders})
        GROUP BY entity_id
        ORDER BY n DESC
        LIMIT 3
        """,
        entity_ids,
    ).fetchall()
    top = []
    for entity_id, n in top_rows:
        bg = entity_id.split(":", 1)[1]
        e = entities.get(store, bg)
        if e:
            top.append({"entity": e, "count": int(n)})
    return {
        "members": len(bioguide_ids),
        "by_party": by_party,
        "votes": votes, "breaks": breaks,
        "sponsored": sponsored, "cosponsored": cosponsored,
        "speeches": speeches,
        "top": top,
    }


def _day_payload(store: Store, day: str, *, entity_filter: str | None, limit: int) -> JSONResponse:
    """Aggregate events that occurred on ``day`` (ISO YYYY-MM-DD, UTC).

    Optionally restricts to a single entity (e.g. "bioguide:X000000"). Returns
    grouped items + per-type counts. Used by both /api/day/site and
    /api/day/legislator endpoints.
    """
    import json as _json
    from datetime import datetime as _dt

    try:
        d = _dt.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        store.close()
        raise HTTPException(400, f"bad date: {day}")

    try:
        params: list = [d, d + timedelta(days=1)]
        where = "CAST(occurred_at AT TIME ZONE 'UTC' AS DATE) >= ? AND CAST(occurred_at AT TIME ZONE 'UTC' AS DATE) < ?"
        if entity_filter:
            where += " AND entity_id = ?"
            params.append(entity_filter)
        else:
            where += " AND entity_id LIKE 'bioguide:%'"

        rows = store.conn.execute(
            f"""
            SELECT occurred_at, entity_id, event_type, payload
            FROM events
            WHERE {where}
            ORDER BY occurred_at DESC
            """,
            params,
        ).fetchall()

        # Resolve legislator names in one pass for site-wide scope
        bioguides_seen: set[str] = set()
        for _, entity_id, _, _ in rows:
            if entity_id and entity_id.startswith("bioguide:"):
                bioguides_seen.add(entity_id.split(":", 1)[1])
        name_map: dict[str, dict] = {}
        for bg in bioguides_seen:
            ent = entities.get(store, bg)
            if ent:
                name_map[bg] = {
                    "bioguide": bg,
                    "name": ent.full_name,
                    "party": (ent.party[0] if ent.party else "I"),
                    "url": f"/legislator/{bg}",
                }

        from conductor.politics.bills import parse_legis_num
        counts: dict[str, int] = {}
        items: list[dict] = []
        for occ, entity_id, etype, payload in rows:
            counts[etype] = counts.get(etype, 0) + 1
            if len(items) >= limit:
                continue
            pl = _json.loads(payload) if isinstance(payload, str) else (payload or {})
            who = None
            if entity_id and entity_id.startswith("bioguide:"):
                who = name_map.get(entity_id.split(":", 1)[1])
            item = {
                "event_type": etype,
                "when": occ.isoformat() if hasattr(occ, "isoformat") else str(occ),
                "who": who,
                "title": pl.get("title") or pl.get("question") or "",
                "position": pl.get("position"),
                "party_line": pl.get("party_line"),
            }
            # Bill / roll-call linkbacks
            bid = pl.get("bill_id")
            if bid and ":" in bid:
                parts = bid.split(":")
                if len(parts) == 3:
                    item["bill_url"] = f"/bill/{parts[0]}/{parts[1]}/{parts[2]}"
                    item["bill_label"] = f"{parts[1].upper()} {parts[2]}"
            elif pl.get("legis_num") and pl.get("congress"):
                parsed = parse_legis_num(pl.get("legis_num"))
                if parsed:
                    item["bill_url"] = f"/bill/{pl['congress']}/{parsed[0]}/{parsed[1]}"
                    item["bill_label"] = pl.get("legis_num")
            items.append(item)
    finally:
        store.close()

    return JSONResponse({
        "date": d.isoformat(),
        "scope": entity_filter or "site",
        "total": sum(counts.values()),
        "counts": counts,
        "items": items,
        "truncated": sum(counts.values()) > len(items),
    })


def create_app(db_path: Path | None = None) -> FastAPI:
    app = FastAPI(title="TallyHQ", docs_url="/docs")
    env = _env()
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def get_store() -> Store:
        return Store(db_path) if db_path else Store()

    own_host = os.environ.get("ANALYTICS_OWN_HOST", "tallyhq.org")

    @app.middleware("http")
    async def _record_page_view(request: Request, call_next):
        """Record one row in analytics.duckdb per page request.

        Runs OUTSIDE _duckdb_lock_to_503 so it sees the final response (incl.
        any 503 we synthesized). Skips static assets, JSON APIs, and infra
        routes via analytics.should_skip. Errors here are swallowed — analytics
        must never break a request.
        """
        if analytics_mod.should_skip(request.url.path):
            return await call_next(request)
        t0 = time.perf_counter()
        response = await call_next(request)
        try:
            ua = request.headers.get("user-agent", "") or ""
            bot = analytics_mod.is_bot(ua) or analytics_mod.is_probe_path(request.url.path)
            ip = analytics_mod.client_ip(request)
            ref_host = analytics_mod.referer_host(
                request.headers.get("referer"), own_host
            )
            vh = analytics_mod.visitor_hash(ip, ua) if not bot else None
            dur_ms = int((time.perf_counter() - t0) * 1000)
            store = analytics_mod.AnalyticsStore()
            try:
                store.record(
                    path=request.url.path,
                    status=response.status_code,
                    method=request.method,
                    referer_host=ref_host,
                    ua=ua,
                    is_bot=bot,
                    visitor_hash=vh,
                    dur_ms=dur_ms,
                )
            finally:
                store.close()
        except Exception as e:
            import logging as _logging
            _logging.getLogger("conductor.analytics").warning(
                "page_view record failed: %s", e
            )
        return response

    @app.middleware("http")
    async def _duckdb_lock_to_503(request: Request, call_next):
        """Daily-update spawns a subprocess that holds the DuckDB write-lock for
        the duration of the run. Web requests that try to open the DB during
        that window crash with `_duckdb.IOException: Could not set lock ...`.
        Convert that to a fast 503 + Retry-After so the client sees a clean
        "syncing in progress" instead of a 500.
        """
        import duckdb as _duckdb
        try:
            return await call_next(request)
        except _duckdb.IOException as e:
            msg = str(e)
            if "Could not set lock" in msg or "Conflicting lock" in msg:
                return JSONResponse(
                    {"error": "syncing", "detail": "daily-update in progress, retry shortly"},
                    status_code=503,
                    headers={"Retry-After": "60"},
                )
            raise

    @app.get("/", response_class=HTMLResponse)
    def landing(q: str | None = Query(None, max_length=80)):
        # Header search submits to "/" — punt to /browse for the actual results
        # so the full filter context (committees, aggregates) renders.
        if q:
            return RedirectResponse(url=f"/browse?q={q}", status_code=302)
        store = get_store()
        try:

            totals = landing_mod.totals(store)
            term = _congress_term_context()
            agg = landing_mod.aggregate_grid(store, days=180)
            agg_svg = render_svg(agg, cell_size=22, cell_gap=4, interactive=True)
            pulse = landing_mod.pulse_stats(store, days=180)
            recent_bills = landing_mod.recent_bills(store, limit=8)
            top_cosponsored = landing_mod.most_cosponsored(store, limit=5)
            top_lobbied = lobby_views.most_lobbied_bills(store, limit=8)
            most_active = landing_mod.most_active(store, days=180, limit=8)
            most_absent = landing_mod.most_absent(store, days=180, limit=8)
            breakers = landing_mod.biggest_breakers(store, days=180, min_votes=30, limit=8)
            breaks = landing_mod.recent_breaks(store, limit=10)
        finally:
            store.close()
        tmpl = env.get_template("landing.html")
        return tmpl.render(
            totals=totals,
            term=term,
            agg_svg=agg_svg,
            agg_total=agg.total,
            pulse=pulse,
            recent_bills=recent_bills,
            top_cosponsored=top_cosponsored,
            top_lobbied=top_lobbied,
            most_active=most_active,
            most_absent=most_absent,
            breakers=breakers,
            breaks=breaks,
            photo=lambda b: photo_url(b, "225x275"),
        )

    @app.get("/browse", response_class=HTMLResponse)
    def browse(
        chamber: str | None = Query(None, pattern="^(house|senate)$"),
        state: str | None = Query(None, max_length=2),
        party: str | None = Query(None, max_length=20),
        q: str | None = Query(None, max_length=80),
        committee: str | None = Query(None, max_length=20),
    ):
        store = get_store()
        try:
            roster = entities.list_all(store, chamber=chamber, state=(state.upper() if state else None))
            if party:
                roster = [r for r in roster if r.party == party]
            if q:
                ql = q.lower()
                roster = [r for r in roster if ql in r.full_name.lower() or ql in r.last_name.lower()]
            committee_obj = None
            if committee:
                committees_mod.ensure_schema(store)
                bgs_on = set(committees_mod.on_committee(store, committee))
                roster = [r for r in roster if r.bioguide_id in bgs_on]
                row = store.conn.execute(
                    "SELECT name, chamber, committee_type, parent_code FROM committees WHERE committee_code = ?",
                    [committee],
                ).fetchone()
                if row:
                    committee_obj = {"code": committee, "name": row[0], "chamber": row[1],
                                     "type": row[2], "parent_code": row[3]}

            agg = _aggregate_stats(store, [r.bioguide_id for r in roster])
            committees_list = _committee_options(store)
        finally:
            store.close()
        tmpl = env.get_template("browse.html")
        return tmpl.render(
            roster=roster, count=len(roster),
            chamber=chamber, state=state, party=party, q=q,
            committee=committee, committee_obj=committee_obj,
            committees_list=committees_list,
            agg=agg,
            photo=lambda b: photo_url(b, "225x275"),
        )

    @app.get("/legislator/{bioguide}", response_class=HTMLResponse)
    def legislator(
        bioguide: str,
        days: int = Query(365, ge=7, le=730),
        tab: str = Query("overview", pattern="^(overview|votes|bills|speeches)$"),
        session_compare: int = Query(0, ge=0, le=1),
    ):
        store = get_store()
        try:
            ent = entities.get(store, bioguide)
            if ent is None:
                raise HTTPException(404, f"unknown bioguide: {bioguide}")
            end = date.today()
            start = end - timedelta(days=days)
            row = grid(store, ent.entity_id, start, end)
            svg = render_svg(row, interactive=True)
            session_compare_svgs = None
            if session_compare:
                from conductor.politics.session_calendar import session_mask
                mask = session_mask(start, end)
                session_compare_svgs = {
                    "baseline": render_svg(row, interactive=True),
                    "recess_bg": render_svg(row, interactive=True,
                                            session_mask=mask, session_mode="recess-bg"),
                    "stripe": render_svg(row, interactive=True,
                                         session_mask=mask, session_mode="stripe"),
                }
            stats = stats_mod.compute(store, ent.entity_id)
            committees = committees_mod.member_committees(store, ent.bioguide_id)
            role = committees_mod.leadership_roles(store, [ent.bioguide_id]).get(ent.bioguide_id)
            pvi = pvi_mod.for_member(store, ent.state, ent.district, ent.chamber)
            funding_rows = funding_mod.for_member(store, ent.bioguide_id)
            social = social_mod.get(store, ent.bioguide_id)

            # Per-tab feed query — different event_type filter + limit
            tab_filters = {
                "overview": (None, 30),
                "votes":    ("event_type IN ('vote.cast','vote.missed')", 80),
                "bills":    ("event_type IN ('bill.sponsored','bill.cosponsored')", 80),
                "speeches": ("event_type = 'floor.speech'", 80),
            }
            where_extra, limit = tab_filters[tab]
            sql = """
                SELECT occurred_at, event_type, payload
                FROM events
                WHERE entity_id = ?
            """
            if where_extra:
                sql += f" AND {where_extra}"
            sql += " ORDER BY occurred_at DESC LIMIT ?"
            recent = store.conn.execute(sql, [ent.entity_id, limit]).fetchall()
        finally:
            store.close()
        tmpl = env.get_template("legislator.html")
        return tmpl.render(
            entity=ent,
            grid=row,
            svg=svg,
            stats=stats,
            committees=committees,
            role=role,
            pvi=pvi,
            funding=funding_rows,
            social=social,
            recent=recent,
            days=days,
            tab=tab,
            photo=photo_url(ent.bioguide_id, "450x550"),
            session_compare_svgs=session_compare_svgs,
        )

    @app.get("/legislator/{bioguide}.svg")
    def legislator_svg(bioguide: str, days: int = Query(365, ge=7, le=1095)):
        store = get_store()
        try:
            ent = entities.get(store, bioguide)
            if ent is None:
                raise HTTPException(404, f"unknown bioguide: {bioguide}")
            end = date.today()
            start = end - timedelta(days=days)
            row = grid(store, ent.entity_id, start, end)
            svg = render_svg(row)
        finally:
            store.close()
        return Response(content=svg, media_type="image/svg+xml")

    @app.get("/bills", response_class=HTMLResponse)
    def bills_index(
        q: str | None = Query(None, max_length=120),
        bill_type: str | None = Query(None, max_length=10),
        kind: str | None = Query(None, pattern="^(bills|joint|concurrent|simple)$"),
        chamber: str | None = Query(None, pattern="^(house|senate)$"),
        policy_area: str | None = Query(None, max_length=80),
        sort: str = Query("recent", pattern="^(recent|introduced|cosponsors|title)$"),
        page: int = Query(1, ge=1, le=200),
    ):
        store = get_store()
        try:
            page_size = 50
            offset = (page - 1) * page_size

            where_clauses: list[str] = []
            params: list = []
            if q:
                where_clauses.append("LOWER(title) LIKE ?")
                params.append(f"%{q.lower()}%")
            if bill_type:
                where_clauses.append("bill_type = ?")
                params.append(bill_type.lower())
            if kind:
                kind_map = {
                    "bills":       ("hr", "s"),
                    "joint":       ("hjres", "sjres"),
                    "concurrent":  ("hconres", "sconres"),
                    "simple":      ("hres", "sres"),
                }
                kinds = kind_map[kind]
                placeholders = ",".join(["?"] * len(kinds))
                where_clauses.append(f"bill_type IN ({placeholders})")
                params.extend(kinds)
            if chamber:
                # House bill types start with 'h', Senate with 's'
                if chamber == "house":
                    where_clauses.append("bill_type LIKE 'h%'")
                else:
                    where_clauses.append("bill_type LIKE 's%'")
            if policy_area:
                where_clauses.append("policy_area = ?")
                params.append(policy_area)

            where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

            order_sql = {
                "recent": "ORDER BY COALESCE(latest_action_date, introduced_date) DESC NULLS LAST",
                "introduced": "ORDER BY introduced_date DESC NULLS LAST",
                "cosponsors": "ORDER BY cosponsor_count DESC, COALESCE(latest_action_date, introduced_date) DESC NULLS LAST",
                "title": "ORDER BY title ASC",
            }[sort]

            total = store.conn.execute(
                f"SELECT COUNT(*) FROM bills {where_sql}", params
            ).fetchone()[0]

            rows = store.conn.execute(
                f"""
                SELECT bill_id, congress, bill_type, number, title, sponsor_bioguide,
                       introduced_date, latest_action_date, latest_action_text,
                       policy_area, cosponsor_count
                FROM bills
                {where_sql}
                {order_sql}
                LIMIT ? OFFSET ?
                """,
                [*params, page_size, offset],
            ).fetchall()

            from conductor.politics import entities as ent_mod
            results = []
            for r in rows:
                sponsor = ent_mod.get(store, r[5]) if r[5] else None
                results.append({
                    "bill_id": r[0], "congress": r[1], "bill_type": r[2], "number": r[3],
                    "title": r[4] or "", "sponsor": sponsor,
                    "introduced_date": r[6], "latest_action_date": r[7],
                    "latest_action_text": r[8] or "", "policy_area": r[9] or "",
                    "cosponsor_count": int(r[10] or 0),
                })

            policy_areas = [
                row[0] for row in store.conn.execute(
                    "SELECT DISTINCT policy_area FROM bills WHERE policy_area IS NOT NULL AND policy_area <> '' ORDER BY 1"
                ).fetchall()
            ]

            agg = store.conn.execute(
                f"""
                SELECT COUNT(*) total, SUM(cosponsor_count) cosp, COUNT(DISTINCT sponsor_bioguide) sponsors
                FROM bills {where_sql}
                """, params
            ).fetchone()
        finally:
            store.close()

        tmpl = env.get_template("bills.html")
        total_pages = (total + page_size - 1) // page_size
        return tmpl.render(
            results=results,
            total=total,
            page=page, page_size=page_size, total_pages=total_pages,
            q=q, bill_type=bill_type, kind=kind, chamber=chamber, policy_area=policy_area, sort=sort,
            policy_areas=policy_areas,
            agg={
                "total": int(agg[0] or 0),
                "cosp": int(agg[1] or 0),
                "sponsors": int(agg[2] or 0),
            },
            photo=lambda b: photo_url(b, "225x275"),
        )

    @app.post(
        "/bill/{congress}/{bill_type}/{number}/changelog/{from_code}/{to_code}/summary",
        response_class=HTMLResponse,
    )
    def bill_summary_generate(congress: int, bill_type: str, number: int,
                              from_code: str, to_code: str):
        """Generate (or return cached) AI summary for one diff. Only allowed
        for bills that have passed both chambers (enrolled or law). Returns
        rendered card HTML.
        """
        bill_id = f"{congress}:{bill_type}:{number}"
        store = get_store()
        try:
            if not _is_passed(store, bill_id):
                raise HTTPException(403, "AI summaries only available for passed bills")
            from_body = bill_text_mod.get_body_db(store, bill_id, from_code) or ""
            to_body = bill_text_mod.get_body_db(store, bill_id, to_code) or ""
            if not (from_body and to_body):
                raise HTTPException(404, f"text not available for {from_code}/{to_code}")
            from_label = bill_text_mod.STAGE_LABELS.get(from_code, from_code)
            to_label = bill_text_mod.STAGE_LABELS.get(to_code, to_code)
            res = bill_summary_mod.summarize_delta(
                store, bill_id, from_code, to_code,
                from_label, to_label, from_body, to_body,
            )
            if res is None:
                raise HTTPException(503, "AI summarizer unavailable (OPENROUTER_API_KEY not set)")
            sha_pair = bill_summary_mod.sha256_pair(from_body, to_body)
            cached = bill_summary_mod.get_cached(
                store, bill_id, from_code, to_code, sha_pair,
            )
        finally:
            store.close()
        tmpl = env.get_template("_ai_summary_card.html")
        return tmpl.render(ai_summary=cached)

    @app.get(
        "/bill/{congress}/{bill_type}/{number}/changelog/{from_code}/{to_code}",
        response_class=HTMLResponse,
    )
    def bill_changelog_diff(congress: int, bill_type: str, number: int,
                            from_code: str, to_code: str):
        """Render a single from→to diff as a partial page (full HTML w/ chrome)."""
        bill_id = f"{congress}:{bill_type}:{number}"
        store = get_store()
        try:
            b = bills_mod.get(store, bill_id)
            if b is None:
                raise HTTPException(404, f"unknown bill {bill_id}")
            res = bill_text_mod.load_diff(store, bill_id, from_code, to_code)
            changelog = bill_text_mod.changelog_for_bill(store, bill_id)
            # AI summary lookup (cache only — never trigger generation from a
            # web request to avoid surprise LLM bills under traffic).
            from_body = bill_text_mod.get_body_db(store, bill_id, from_code) or ""
            to_body = bill_text_mod.get_body_db(store, bill_id, to_code) or ""
            sha_pair = bill_summary_mod.sha256_pair(from_body, to_body)
            cached_summary = bill_summary_mod.get_cached(
                store, bill_id, from_code, to_code, sha_pair,
            )
            is_passed = _is_passed(store, bill_id)
        finally:
            store.close()
        if res is None:
            raise HTTPException(404, f"text not available for {from_code} or {to_code}")
        diff_html, stats = res
        tmpl = env.get_template("bill_diff.html")
        return tmpl.render(
            bill=b,
            from_code=from_code, to_code=to_code,
            from_label=bill_text_mod.STAGE_LABELS.get(from_code, from_code),
            to_label=bill_text_mod.STAGE_LABELS.get(to_code, to_code),
            diff_html=diff_html,
            added=stats.added, removed=stats.removed, unchanged=stats.unchanged,
            changelog=changelog,
            ai_summary=cached_summary,
            is_passed=is_passed,
        )

    @app.get("/bill/{congress}/{bill_type}/{number}", response_class=HTMLResponse)
    def bill_detail(congress: int, bill_type: str, number: int):
        bill_id = f"{congress}:{bill_type}:{number}"
        store = get_store()
        try:
            b = bills_mod.get(store, bill_id)

            # Bill not in our index yet — many roll-call votes reference bills
            # we haven't bulk-loaded. Render a stub instead of 404 so links
            # from legislator profiles never dead-end.
            if b is None:
                tallies = bill_views.rollcall_tallies(store, bill_id)
                long_type = {
                    "hr": "house-bill", "s": "senate-bill",
                    "hjres": "house-joint-resolution",
                    "sjres": "senate-joint-resolution",
                    "hconres": "house-concurrent-resolution",
                    "sconres": "senate-concurrent-resolution",
                    "hres": "house-resolution",
                    "sres": "senate-resolution",
                }.get(bill_type, bill_type)
                external_url = (
                    f"https://www.congress.gov/bill/{congress}th-congress/"
                    f"{long_type}/{number}"
                )
                tmpl = env.get_template("bill.html")
                return tmpl.render(
                    bill=None,
                    bill_id=bill_id,
                    bill_stub={
                        "congress": congress,
                        "bill_type": bill_type,
                        "number": number,
                        "url": external_url,
                    },
                    sponsor=None,
                    cosponsors=[],
                    tallies=tallies,
                    actions=[],
                    roles={},
                    photo=lambda bg: photo_url(bg, "225x275"),
                    big_photo=lambda bg: photo_url(bg, "450x550"),
                )

            sponsor = bill_views.sponsor(store, b)
            cosponsors = bill_views.cosponsors(store, bill_id)
            tallies = bill_views.rollcall_tallies(store, bill_id)
            actions = _fetch_bill_actions(store, bill_id)
            role_bgs: list[str] = []
            if sponsor:
                role_bgs.append(sponsor.bioguide_id)
            role_bgs.extend(c.entity.bioguide_id for c in cosponsors)
            roles = committees_mod.leadership_roles(store, role_bgs)
            lobby_clients = lobby_views.top_clients_for_bill(store, bill_id, limit=10)
            text_versions = _format_text_versions(b.text_versions)
            crs_summaries = _fetch_crs_summaries(store, bill_id)
            changelog = bill_text_mod.changelog_for_bill(store, bill_id)
            default_diff_html = None
            default_diff_stats = None
            default_ai_summary = None
            if changelog["default_from"] and changelog["default_to"] \
                    and changelog["default_from"] != changelog["default_to"]:
                res = bill_text_mod.load_diff(
                    store, bill_id,
                    changelog["default_from"], changelog["default_to"],
                )
                if res is not None:
                    default_diff_html, default_diff_stats = res
                # cache-only summary lookup
                from_body = bill_text_mod.get_body_db(
                    store, bill_id, changelog["default_from"]) or ""
                to_body = bill_text_mod.get_body_db(
                    store, bill_id, changelog["default_to"]) or ""
                sha_pair = bill_summary_mod.sha256_pair(from_body, to_body)
                default_ai_summary = bill_summary_mod.get_cached(
                    store, bill_id,
                    changelog["default_from"], changelog["default_to"],
                    sha_pair,
                )
            is_passed = _is_passed(store, bill_id)
        finally:
            store.close()
        tmpl = env.get_template("bill.html")
        return tmpl.render(
            bill=b,
            text_versions=text_versions,
            bill_stub=None,
            sponsor=sponsor,
            cosponsors=cosponsors,
            tallies=tallies,
            actions=actions,
            lobby_clients=lobby_clients,
            crs_summaries=crs_summaries,
            changelog=changelog,
            default_diff_html=default_diff_html,
            default_diff_stats=default_diff_stats,
            default_ai_summary=default_ai_summary,
            is_passed=is_passed,
            roles=roles,
            photo=lambda bg: photo_url(bg, "225x275"),
            big_photo=lambda bg: photo_url(bg, "450x550"),
        )

    PLACEHOLDER_SVG = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 122'>"
        "<rect width='100%' height='100%' fill='#f3efe7'/>"
        "<circle cx='50' cy='44' r='18' fill='#c9c4b8'/>"
        "<rect x='22' y='70' width='56' height='40' rx='4' fill='#c9c4b8'/>"
        "</svg>"
    )

    @app.get("/rollcall/house/{year}/{num}", response_class=HTMLResponse)
    def rollcall_house(year: int, num: int):
        store = get_store()
        try:
            d = rollcall_views.get_house_rollcall(store, year, num)
            if d is None:
                raise HTTPException(404, f"unknown House roll-call {year}/{num}")
        finally:
            store.close()
        tmpl = env.get_template("rollcall.html")
        return tmpl.render(d=d, photo=lambda b: photo_url(b, "225x275"))

    @app.get("/rollcall/senate/{congress}/{session}/{num}", response_class=HTMLResponse)
    def rollcall_senate(congress: int, session: int, num: int):
        store = get_store()
        try:
            d = rollcall_views.get_senate_rollcall(store, congress, session, num)
            if d is None:
                raise HTTPException(404, f"unknown Senate roll-call {congress}/{session}/{num}")
        finally:
            store.close()
        tmpl = env.get_template("rollcall.html")
        return tmpl.render(d=d, photo=lambda b: photo_url(b, "225x275"))

    @app.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
    def robots():
        return (
            "User-agent: *\n"
            "Allow: /\n"
            "Disallow: /api/\n"
            "Disallow: /docs\n"
            "Sitemap: https://tallyhq.org/sitemap.xml\n"
        )

    @app.get("/sitemap.xml", include_in_schema=False)
    def sitemap():
        store = get_store()
        try:
            members = entities.list_all(store, active_only=True)
            bills_rows = store.conn.execute(
                """
                SELECT bill_id, congress, bill_type, number,
                       COALESCE(latest_action_date, introduced_date) AS d
                FROM bills
                ORDER BY d DESC NULLS LAST
                LIMIT 5000
                """
            ).fetchall()
        finally:
            store.close()
        urls: list[tuple[str, str]] = [
            ("https://tallyhq.org/", "1.0"),
            ("https://tallyhq.org/browse", "0.9"),
            ("https://tallyhq.org/bills", "0.9"),
        ]
        for m in members:
            urls.append((f"https://tallyhq.org/legislator/{m.bioguide_id}", "0.7"))
        for r in bills_rows:
            urls.append((
                f"https://tallyhq.org/bill/{r[1]}/{r[2]}/{r[3]}",
                "0.5",
            ))
        body = ['<?xml version="1.0" encoding="UTF-8"?>']
        body.append('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">')
        for url, prio in urls:
            body.append(f"<url><loc>{url}</loc><priority>{prio}</priority></url>")
        body.append("</urlset>")
        return Response(content="\n".join(body), media_type="application/xml")

    @app.get("/lobby/client/{client_id}", response_class=HTMLResponse)
    def lobby_client(client_id: str):
        store = get_store()
        try:
            profile = lobby_views.get_client(store, client_id)
            if profile is None:
                raise HTTPException(404, f"unknown client: {client_id}")
            bills = lobby_views.bills_for_client(store, client_id, limit=50)
            registrants = lobby_views.registrants_for_client(store, client_id, limit=20)
        finally:
            store.close()
        tmpl = env.get_template("lobby_client.html")
        return tmpl.render(profile=profile, bills=bills, registrants=registrants)

    @app.get("/api/search")
    def api_search(q: str = Query("", max_length=80), limit: int = Query(8, ge=1, le=20)):
        """Combined autocomplete — members + bills.

        Members: substring match on full_name OR last_name OR state.
        Bills:   substring match on title (LOWER), capped to current Congress.
        Cheap at our scale; no FTS needed.
        """
        ql = q.strip().lower()
        if not ql or len(ql) < 2:
            return JSONResponse({"members": [], "bills": [], "q": q})

        store = get_store()
        try:
            # Members — pull full roster, filter in Python (small N)
            members_rows = store.conn.execute(
                """
                SELECT bioguide_id, full_name, last_name, party, state, district, chamber
                FROM legislators
                WHERE LOWER(full_name) LIKE ?
                   OR LOWER(last_name) LIKE ?
                   OR LOWER(state) = ?
                ORDER BY chamber, last_name
                LIMIT ?
                """,
                [f"%{ql}%", f"%{ql}%", ql, limit],
            ).fetchall()
            members = [
                {
                    "bioguide": r[0],
                    "name": r[1],
                    "party": r[3] or "",
                    "party_letter": (r[3][0] if r[3] else "I"),
                    "state": r[4] or "",
                    "district": int(r[5]) if r[5] is not None and r[5] != 0 else None,
                    "chamber": r[6] or "",
                    "photo_url": photo_url(r[0], "225x275"),
                    "url": f"/legislator/{r[0]}",
                }
                for r in members_rows
            ]

            # Bills — match by:
            #   1. exact bill_type + number  (e.g. "hr 707", "sres 707", "s.res. 707")
            #   2. pure number               (e.g. "707" → matches across all types)
            #   3. title substring (LIKE)
            import re as _re
            bill_clauses: list[str] = []
            bill_params: list = []

            # Match "sres 707", "S.Res. 707", "hr707", "H R 707" — flexible
            tn = _re.match(
                r"^\s*(hr|s|hres|sres|hjres|sjres|hconres|sconres|"
                r"h\.?\s*r\.?|h\.?\s*res\.?|s\.?\s*res\.?|"
                r"h\.?\s*j\.?\s*res\.?|s\.?\s*j\.?\s*res\.?|"
                r"h\.?\s*con\.?\s*res\.?|s\.?\s*con\.?\s*res\.?)"
                r"\s*\.?\s*(\d{1,5})\s*$",
                ql, _re.I,
            )
            parsed = None
            if tn:
                raw_type = _re.sub(r"[\.\s]", "", tn.group(1)).lower()
                bt = {
                    "hr": "hr", "s": "s",
                    "hres": "hres", "sres": "sres",
                    "hjres": "hjres", "sjres": "sjres",
                    "hconres": "hconres", "sconres": "sconres",
                }.get(raw_type)
                if bt:
                    parsed = (bt, int(tn.group(2)))
                    bill_clauses.append("(bill_type = ? AND number = ?)")
                    bill_params.extend([bt, parsed[1]])

            stripped = ql.replace(".", "").replace(" ", "")
            if stripped.isdigit():
                bill_clauses.append("number = ?")
                bill_params.append(int(stripped))

            bill_clauses.append("LOWER(title) LIKE ?")
            bill_params.append(f"%{ql}%")

            where_sql = " OR ".join(bill_clauses)
            bills_rows = store.conn.execute(
                f"""
                SELECT bill_id, congress, bill_type, number, title,
                       sponsor_bioguide, cosponsor_count, latest_action_date,
                       CASE
                         WHEN bill_type = ? AND number = ? THEN 0
                         WHEN number = ? THEN 1
                         ELSE 2
                       END AS rank
                FROM bills
                WHERE {where_sql}
                ORDER BY rank ASC, COALESCE(latest_action_date, introduced_date) DESC NULLS LAST
                LIMIT ?
                """,
                [
                    parsed[0] if parsed else "",
                    int(parsed[1]) if parsed else -1,
                    int(stripped) if stripped.isdigit() else -1,
                    *bill_params,
                    limit,
                ],
            ).fetchall()
            bills = [
                {
                    "bill_id": r[0],
                    "congress": int(r[1]),
                    "bill_type": r[2],
                    "number": int(r[3]),
                    "title": (r[4] or "")[:140],
                    "cosponsor_count": int(r[6] or 0),
                    "url": f"/bill/{r[1]}/{r[2]}/{r[3]}",
                }
                for r in bills_rows
            ]
        finally:
            store.close()
        return JSONResponse({"members": members, "bills": bills, "q": q})

    @app.get("/photo/{bioguide}")
    def photo(bioguide: str, size: str = "225x275"):
        if size not in ("original", "450x550", "225x275"):
            size = "225x275"
        url = photos_mod.resolve(bioguide, size)
        if url == photos_mod.PLACEHOLDER:
            return Response(
                content=PLACEHOLDER_SVG,
                media_type="image/svg+xml",
                headers={"Cache-Control": "public, max-age=86400"},
            )
        return RedirectResponse(
            url=url,
            status_code=302,
            headers={"Cache-Control": "public, max-age=86400"},
        )

    @app.get("/stats", response_class=HTMLResponse)
    def stats_page(
        token: str = Query("", max_length=128),
        days: int = Query(7, ge=1, le=90),
        include_bots: int = Query(0, ge=0, le=1),
    ):
        """Visitor stats. Gated by `ANALYTICS_TOKEN` env var — pass via ?token=
        query string. If env unset, the route 404s (off by default).
        """
        expected = os.environ.get("ANALYTICS_TOKEN", "").strip()
        if not expected or token != expected:
            raise HTTPException(404)
        store = analytics_mod.AnalyticsStore()
        try:
            s = store.summary(days=days, exclude_bots=not bool(include_bots))
        finally:
            store.close()
        tmpl = env.get_template("stats.html")
        return tmpl.render(s=s, token=token, days=days,
                           include_bots=bool(include_bots))

    @app.on_event("startup")
    async def _schedule_daily_update():
        """In-process daily-update scheduler.

        Set DAILY_UPDATE_HOUR_UTC env var to enable (e.g. "7" = 07:00 UTC).
        When unset, this loop is a no-op — useful for local dev or when
        you've split cron into a separate Railway service.

        Spawns the same logic as `conductor politics daily-update` as a
        subprocess so the DuckDB lock isn't held by the web event loop.
        """
        import asyncio
        import logging as _logging
        import os as _os
        from datetime import datetime, time as _time, timedelta, timezone as _tz

        hour_env = _os.environ.get("DAILY_UPDATE_HOUR_UTC", "").strip()
        if not hour_env:
            return
        try:
            target_hour = int(hour_env)
        except ValueError:
            return
        if not 0 <= target_hour <= 23:
            return

        _log = _logging.getLogger("conductor.daily")
        _log.info("daily-update scheduler enabled — fires at %02d:00 UTC", target_hour)

        async def _loop():
            while True:
                now = datetime.now(tz=_tz.utc)
                target = datetime.combine(now.date(), _time(target_hour, 0), tzinfo=_tz.utc)
                if now >= target:
                    target = target + timedelta(days=1)
                wait_s = (target - now).total_seconds()
                _log.info("next daily-update in %.1fh at %s", wait_s / 3600, target.isoformat())
                await asyncio.sleep(wait_s)
                try:
                    import sys as _sys
                    db_arg = str(db_path) if db_path else "/data/conductor.duckdb"
                    proc = await asyncio.create_subprocess_exec(
                        _sys.executable, "-m", "conductor.cli",
                        "--db", db_arg, "politics", "daily-update",
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                    )
                    out, _ = await proc.communicate()
                    _log.info("daily-update exited %s\n%s", proc.returncode,
                              (out or b"").decode("utf-8", errors="replace")[:4000])
                except Exception as e:
                    _log.error("daily-update spawn failed: %s", e)

        asyncio.create_task(_loop())

    @app.on_event("startup")
    async def _run_backfill_adapters():
        """One-shot backfill applied at boot.

        Set BACKFILL_ADAPTERS env to a comma list of adapter names (e.g.
        "legislators-social") to run them once on next boot. Writes a marker
        file so re-restarts don't re-run; bump the env value to force again.

        Mirrors the RESET_CURSORS shape: set, deploy, marker prevents
        re-firing, eventually unset the env to clean up.
        """
        import logging as _logging
        spec = os.environ.get("BACKFILL_ADAPTERS", "").strip()
        if not spec:
            return
        _log = _logging.getLogger("conductor.backfill")
        marker_dir = (db_path.parent if db_path else Path("/data"))
        marker = marker_dir / f".backfill-{abs(hash(spec)) % (10**12)}.applied"
        if marker.exists():
            _log.info("BACKFILL_ADAPTERS: marker present, skipping (%s)", marker.name)
            return
        names = [n.strip() for n in spec.split(",") if n.strip()]
        store = get_store()
        try:
            for n in names:
                try:
                    if n in ("legislators-social", "legislators-social-sync"):
                        from conductor.politics import legislators_social_sync as lss
                        rows = lss.sync(store)
                        _log.info("backfill %s: %d rows", n, rows)
                    elif n in ("legislators-sync", "legislators"):
                        from conductor.politics import legislators_sync as ls
                        rows = ls.sync(store)
                        _log.info("backfill %s: %d rows", n, rows)
                    elif n in ("committees-sync", "committees"):
                        from conductor.politics import committees_sync as cs
                        rows = cs.sync(store)
                        _log.info("backfill %s: %d rows", n, rows)
                    else:
                        _log.warning("backfill %s: unknown adapter, skipping", n)
                except Exception as e:
                    _log.error("backfill %s: ERROR %s", n, e)
            try:
                marker.parent.mkdir(parents=True, exist_ok=True)
                marker.write_text(spec)
                _log.info("backfill marker written: %s", marker)
            except Exception as e:
                _log.error("could not write backfill marker: %s", e)
        finally:
            store.close()

    @app.on_event("startup")
    async def _apply_cursor_overrides():
        """One-shot cursor override applied at boot.

        Set RESET_CURSORS env to "name1=value1,name2=value2" to force adapter
        cursors to specific values on next boot. Useful after fixing an adapter
        that walked its cursor past good data. Unset the var after the deploy
        completes so subsequent restarts don't keep re-applying.
        """
        import logging as _logging
        import os as _os
        spec = _os.environ.get("RESET_CURSORS", "").strip()
        if not spec:
            return
        _log = _logging.getLogger("conductor.cursor-reset")
        store = get_store()
        try:
            for pair in spec.split(","):
                pair = pair.strip()
                if not pair or "=" not in pair:
                    continue
                name, value = pair.split("=", 1)
                store.set_cursor(name.strip(), value.strip())
                _log.info("cursor reset: %s -> %s", name.strip(), value.strip())
        except Exception as e:
            _log.error("cursor reset failed: %s", e)
        finally:
            store.close()

    @app.on_event("startup")
    async def _warm_photos():
        # Load DB-persisted cache into memory first (instant), then probe any
        # bioguides not yet cached (background, non-blocking).
        import asyncio
        import logging as _logging
        _log = _logging.getLogger("conductor.photos")

        store = get_store()
        try:
            n_loaded = photos_mod.load_persisted_into_memory(store)
            _log.info("photo cache loaded from DB: %d entries", n_loaded)
            roster = entities.list_all(store)
        finally:
            store.close()

        bioguides = [e.bioguide_id for e in roster]

        async def _bg():
            # Open a fresh store handle for the warm task — DuckDB is process-
            # safe but connection objects shouldn't be shared across tasks.
            wstore = get_store()
            try:
                for size in ("225x275", "450x550"):
                    counts = await photos_mod.warm_cache(
                        bioguides, size=size, concurrency=24, store=wstore,
                    )
                    _log.info("photo cache warmed (%s): %s", size, counts)
            finally:
                wstore.close()

        asyncio.create_task(_bg())

    @app.get("/api/legislator/{bioguide}")
    def legislator_api(bioguide: str, days: int = Query(180, ge=7, le=730)):
        store = get_store()
        try:
            ent = entities.get(store, bioguide)
            if ent is None:
                raise HTTPException(404, f"unknown bioguide: {bioguide}")
            end = date.today()
            start = end - timedelta(days=days)
            row = grid(store, ent.entity_id, start, end)
        finally:
            store.close()
        return JSONResponse({
            "bioguide": ent.bioguide_id,
            "name": ent.full_name,
            "chamber": ent.chamber,
            "state": ent.state,
            "party": ent.party,
            "grid": row.to_dict(),
        })

    @app.get("/api/day/site/{day}")
    def api_day_site(day: str, limit: int = Query(40, ge=1, le=200)):
        return _day_payload(get_store(), day, entity_filter=None, limit=limit)

    @app.get("/api/day/legislator/{bioguide}/{day}")
    def api_day_legislator(bioguide: str, day: str, limit: int = Query(80, ge=1, le=400)):
        return _day_payload(get_store(), day, entity_filter=f"bioguide:{bioguide}", limit=limit)

    return app
