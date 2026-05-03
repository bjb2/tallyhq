"""FastAPI app — "GitHub for legislators".

Routes:
  GET /                          roster index, filterable by chamber/state/party
  GET /legislator/{bioguide}     profile page with activity-grid SVG + recent events
  GET /legislator/{bioguide}.svg standalone SVG (embeddable)
  GET /api/legislator/{bioguide} JSON dump of grid + recent events
"""
from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from jinja2 import Environment, FileSystemLoader, select_autoescape

from fastapi.responses import PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from conductor.aggregations.activity_grid import grid
from conductor.politics import (
    bill_views, bills as bills_mod, committees_sync as committees_mod,
    entities, funding as funding_mod, landing as landing_mod,
    lobby_views, photos as photos_mod, rollcall_views, stats as stats_mod,
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
    env.filters["from_json"] = _from_json
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


def create_app(db_path: Path | None = None) -> FastAPI:
    app = FastAPI(title="TallyHQ", docs_url="/docs")
    env = _env()
    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    def get_store() -> Store:
        return Store(db_path) if db_path else Store()

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
            agg_svg = render_svg(agg, cell_size=22, cell_gap=4)
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
    ):
        store = get_store()
        try:
            ent = entities.get(store, bioguide)
            if ent is None:
                raise HTTPException(404, f"unknown bioguide: {bioguide}")
            end = date.today()
            start = end - timedelta(days=days)
            row = grid(store, ent.entity_id, start, end)
            svg = render_svg(row)
            stats = stats_mod.compute(store, ent.entity_id)
            committees = committees_mod.member_committees(store, ent.bioguide_id)
            funding_rows = funding_mod.for_member(store, ent.bioguide_id)

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
            funding=funding_rows,
            recent=recent,
            days=days,
            tab=tab,
            photo=photo_url(ent.bioguide_id, "450x550"),
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
                    photo=lambda bg: photo_url(bg, "225x275"),
                    big_photo=lambda bg: photo_url(bg, "450x550"),
                )

            sponsor = bill_views.sponsor(store, b)
            cosponsors = bill_views.cosponsors(store, bill_id)
            tallies = bill_views.rollcall_tallies(store, bill_id)
            actions = _fetch_bill_actions(store, bill_id)
            lobby_clients = lobby_views.top_clients_for_bill(store, bill_id, limit=10)
        finally:
            store.close()
        tmpl = env.get_template("bill.html")
        return tmpl.render(
            bill=b,
            bill_stub=None,
            sponsor=sponsor,
            cosponsors=cosponsors,
            tallies=tallies,
            actions=actions,
            lobby_clients=lobby_clients,
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

    return app
