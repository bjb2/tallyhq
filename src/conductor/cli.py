"""TallyHQ CLI.

Subcommands:
  pull    <adapter>      Run a pull on an adapter
  list                   List registered adapters
  politics ...           Politics-vertical commands (sync, web, backfill, etc.)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from conductor.adapters import registry  # noqa: F401  (registers adapters via import)
from conductor.http import HttpClient
from conductor.store import Store

logger = logging.getLogger("conductor")


async def _run_pull(adapter_name: str, store: Store) -> int:
    cls = registry.get(adapter_name)
    async with HttpClient() as http:
        adapter = cls(store, http)
        return await adapter.run_pull()


async def _run_enrich(adapter_name: str, store: Store, entity_ids: list[str]) -> int:
    cls = registry.get(adapter_name)
    async with HttpClient() as http:
        adapter = cls(store, http)
        return await adapter.run_enrich(entity_ids)


def cmd_list(_args, _store: Store) -> int:
    for n in registry.names():
        print(n)
    return 0


def cmd_pull(args, store: Store) -> int:
    n = asyncio.run(_run_pull(args.adapter, store))
    print(f"[{args.adapter}] new events: {n}")
    return 0


def cmd_politics_sync(args, store: Store) -> int:
    from conductor.politics.legislators_sync import sync
    n = sync(store)
    print(f"synced {n} legislators")
    return 0


def cmd_politics_grid(args, store: Store) -> int:
    from datetime import date, timedelta
    from conductor.aggregations.activity_grid import grid
    from conductor.politics import entities
    from conductor.politics.render_grid import render_ansi

    bioguide = args.bioguide
    ent = entities.get(store, bioguide)
    if ent is None:
        print(f"unknown bioguide: {bioguide}", file=sys.stderr)
        return 2
    end = date.today()
    start = end - timedelta(days=args.days)
    row = grid(store, ent.entity_id, start, end)
    print(f"{ent.full_name} ({ent.chamber} {ent.state}{'-' + str(ent.district) if ent.district else ''})")
    print(f"days={args.days} total_intensity={row.total:.1f}")
    print(render_ansi(row))
    return 0


def cmd_politics_backfill_bills(args, store: Store) -> int:
    """Loop the bills adapter into a sidecar DB until exhausted or rounds cap hit.

    Lets the main web server keep serving from data/conductor.duckdb while this
    process writes to data/backfill.duckdb. Merge later with `politics merge-backfill`.
    """
    import asyncio
    import shutil
    from pathlib import Path

    store.close()  # don't hold the main DB open

    backfill_path = Path(args.batch_db)
    backfill_path.parent.mkdir(parents=True, exist_ok=True)

    # Seed: copy main schema (events, adapter_cursor, bills) into the backfill DB
    # by opening it fresh (Store ensures schema). Then clear the cursor so we
    # backfill the requested window.
    bf_store = Store(db_path=backfill_path)
    from conductor.politics import bills as bm
    bm.ensure_schema(bf_store)
    if args.reset_cursor:
        bf_store.conn.execute("DELETE FROM adapter_cursor WHERE source = 'congress_bills'")

    from conductor.adapters import registry
    from conductor.http import HttpClient
    cls = registry.get("congress_bills")

    rounds_done = 0
    total_new = 0
    last_round_new = -1

    async def _one_round():
        async with HttpClient() as http:
            adapter = cls(bf_store, http)
            return await adapter.run_pull()

    print(f"backfill -> {backfill_path}  rounds={args.rounds}")
    for i in range(args.rounds):
        try:
            n = asyncio.run(_one_round())
        except Exception as e:
            print(f"round {i+1}: error {e}", file=sys.stderr)
            break
        total_new += n
        rounds_done += 1
        print(f"round {i+1}: +{n} events (cumulative {total_new})", flush=True)
        if n == 0:
            print("no new events — backfill exhausted")
            break
        last_round_new = n

    bf_store.close()
    print(f"done — {rounds_done} rounds, {total_new} new events")
    return 0


def cmd_politics_backfill_lda(args, store: Store) -> int:
    """Backfill Senate LDA filings for one Congress into a sidecar DB.

    Loops every (year, period) tuple for the requested Congress, walking
    paginated results to exhaustion. Writes only to --batch-db so the main
    web app keeps serving from data/conductor.duckdb.
    """
    import asyncio
    from pathlib import Path

    store.close()  # don't hold the main DB

    backfill_path = Path(args.batch_db)
    backfill_path.parent.mkdir(parents=True, exist_ok=True)
    bf_store = Store(db_path=backfill_path)

    from conductor.politics import bills as bm
    from conductor.politics import lobbying as lm
    bm.ensure_schema(bf_store)
    lm.ensure_schema(bf_store)

    from conductor.adapters import registry
    from conductor.adapters.lda_senate import LdaSenateAdapter, _years_for_congress
    from conductor.http import HttpClient

    cls = registry.get("lda_senate")
    years = _years_for_congress(args.congress)

    if args.reset_cursor:
        for y in years:
            for p in ("first_quarter", "second_quarter", "third_quarter", "fourth_quarter"):
                bf_store.conn.execute(
                    "DELETE FROM adapter_cursor WHERE source = ?",
                    [f"lda_senate:{y}:{p}"],
                )

    print(f"backfill LDA congress={args.congress} years={years} -> {backfill_path}")

    async def _run():
        async with HttpClient() as http:
            adapter: LdaSenateAdapter = cls(bf_store, http)  # type: ignore[assignment]
            adapter.years = tuple(years)
            return await adapter.run_pull()

    import time
    t0 = time.time()
    try:
        n = asyncio.run(_run())
    except KeyboardInterrupt:
        print("interrupted — partial progress preserved in cursor", file=sys.stderr)
        bf_store.close()
        return 1

    elapsed = time.time() - t0
    # Pull stats
    total_filings = bf_store.conn.execute(
        "SELECT COUNT(*) FROM lda_filings"
    ).fetchone()[0]
    bill_lobbied = bf_store.conn.execute(
        "SELECT COUNT(*) FROM events WHERE source = 'lda_senate' AND event_type = 'bill_lobbied'"
    ).fetchone()[0]
    resolved = bf_store.conn.execute(
        "SELECT COUNT(*) FROM events "
        "WHERE source = 'lda_senate' AND event_type = 'bill_lobbied' "
        "AND payload::JSON->>'bill_id_resolved' = 'true'"
    ).fetchone()[0]
    bf_store.close()

    print(f"done in {elapsed:.1f}s: {n} new events")
    print(f"  filings: {total_filings}")
    print(f"  bill_lobbied events: {bill_lobbied}")
    if bill_lobbied:
        print(f"  bill resolution rate: {resolved}/{bill_lobbied} = {100*resolved/bill_lobbied:.1f}%")
    return 0


def cmd_politics_merge_backfill(args, store: Store) -> int:
    """Merge a sidecar backfill DB into the main DB.

    Inserts events that don't already exist (by source, source_id, payload_hash)
    and bills that don't already exist (by bill_id).
    """
    from pathlib import Path
    backfill_path = Path(args.batch_db)
    if not backfill_path.exists():
        print(f"backfill DB not found: {backfill_path}", file=sys.stderr)
        return 2

    # Ensure target schema exists
    from conductor.politics import bills as bm, entities as em
    from conductor.politics import lobbying as lm
    bm.ensure_schema(store)
    em.ensure_schema(store)
    lm.ensure_schema(store)

    abs_path = str(backfill_path.resolve()).replace("\\", "/")
    store.conn.execute(f"ATTACH DATABASE '{abs_path}' AS bf (READ_ONLY)")

    # Selective filter — when sidecar is the LDA backfill, the bulky
    # `lda_filing` events duplicate what's already summarized in the
    # `lda_filings` table. --lda-events-only skips them, keeping just
    # `bill_lobbied` events that drive the bill-page lobbying strip.
    extra_filter = ""
    if args.lda_events_only:
        extra_filter = (
            " AND (bfe.source != 'lda_senate' OR bfe.event_type = 'bill_lobbied')"
        )

    before_evs = store.conn.execute("SELECT COUNT(*) FROM main.events").fetchone()[0]
    store.conn.execute(
        f"""
        INSERT INTO main.events
            (source, source_id, entity_id, event_type, observed_at, occurred_at,
             payload_hash, payload, schema_version)
        SELECT source, source_id, entity_id, event_type, observed_at, occurred_at,
               payload_hash, payload, schema_version
        FROM bf.events bfe
        WHERE NOT EXISTS (
            SELECT 1 FROM main.events m
            WHERE m.source = bfe.source
              AND m.source_id = bfe.source_id
              AND m.payload_hash = bfe.payload_hash
        ){extra_filter}
        """
    )
    after_evs = store.conn.execute("SELECT COUNT(*) FROM main.events").fetchone()[0]
    new_events = [(1,)] * (after_evs - before_evs)

    bf_has_bills = store.conn.execute(
        "SELECT COUNT(*) FROM duckdb_tables() "
        "WHERE database_name = 'bf' AND table_name = 'bills'"
    ).fetchone()[0]
    new_bills = []
    if bf_has_bills:
        new_bills = store.conn.execute(
            """
            INSERT INTO main.bills
            SELECT * FROM bf.bills bfb
            WHERE NOT EXISTS (SELECT 1 FROM main.bills m WHERE m.bill_id = bfb.bill_id)
            RETURNING 1
            """
        ).fetchall()

    # lda_filings — only present if the sidecar ran the LDA backfill
    bf_has_lda = store.conn.execute(
        "SELECT COUNT(*) FROM duckdb_tables() "
        "WHERE database_name = 'bf' AND table_name = 'lda_filings'"
    ).fetchone()[0]
    new_lda = []
    if bf_has_lda:
        new_lda = store.conn.execute(
            """
            INSERT INTO main.lda_filings
            SELECT * FROM bf.lda_filings bfl
            WHERE NOT EXISTS (
                SELECT 1 FROM main.lda_filings m
                WHERE m.filing_uuid = bfl.filing_uuid
            )
            RETURNING 1
            """
        ).fetchall()

    store.conn.execute("DETACH bf")
    print(f"merged: {len(new_events)} events, {len(new_bills)} bills, {len(new_lda)} lda_filings")
    return 0


def cmd_politics_lobby_validate(args, store: Store) -> int:
    """Score every bill_lobbied event against its mapped bill and report.

    Identifies likely false positives (regex extracted a bill number that
    points at the wrong bill in the current Congress, usually because the
    lobbyist copied stale boilerplate from an earlier Congress).
    """
    import json as _json
    from collections import Counter

    from conductor.politics import lobby_match

    # The legacy `bill_id_resolved` flag in payloads was set at LDA pull time
    # before bulk-bills had populated the bills table — so it's effectively
    # always 'false' even when the bill_id IS valid. Join to bills directly
    # to find the resolvable subset.
    rows = store.conn.execute(
        """
        SELECT
          json_extract_string(e.payload, '$.bill_id')              AS bill_id,
          json_extract_string(e.payload, '$.client_name')          AS client_name,
          json_extract_string(e.payload, '$.issue_codes_for_bill') AS issue_codes_json,
          json_extract_string(e.payload, '$.mention_text')         AS mention_text
        FROM events e
        JOIN bills b ON b.bill_id = json_extract_string(e.payload, '$.bill_id')
        WHERE e.event_type = 'bill_lobbied'
        """
    ).fetchall()

    if not rows:
        print("no resolved bill_lobbied events to validate")
        return 0

    # Cache bill lookups — one query per distinct bill_id, not per event.
    distinct_bills = {r[0] for r in rows if r[0]}
    bill_meta: dict[str, tuple[str, str]] = {}
    if distinct_bills:
        placeholders = ",".join(["?"] * len(distinct_bills))
        meta_rows = store.conn.execute(
            f"SELECT bill_id, COALESCE(title,''), COALESCE(policy_area,'') "
            f"FROM bills WHERE bill_id IN ({placeholders})",
            list(distinct_bills),
        ).fetchall()
        bill_meta = {m[0]: (m[1], m[2]) for m in meta_rows}

    bands = Counter()
    has_mention_count = 0
    fp_by_bill: Counter = Counter()
    fp_examples: dict[str, list[tuple[str, str]]] = {}

    for bill_id, client_name, ic_json, mention in rows:
        if not bill_id:
            continue
        title, policy = bill_meta.get(bill_id, ("", ""))
        codes: list[str] = []
        try:
            codes = _json.loads(ic_json) if ic_json else []
        except (TypeError, ValueError):
            codes = []
        ms = lobby_match.score_match(
            bill_title=title,
            bill_policy_area=policy,
            issue_codes=codes,
            mention_text=mention or "",
        )
        bands[ms.band] += 1
        if ms.has_mention:
            has_mention_count += 1
        if ms.band == "false_positive":
            fp_by_bill[bill_id] += 1
            ex = fp_examples.setdefault(bill_id, [])
            if len(ex) < 3:
                ex.append((client_name or "?", (mention or "")[:80]))

    total = sum(bands.values())
    print(f"scored {total} resolved bill_lobbied events")
    print(f"  with mention text:  {has_mention_count} ({100*has_mention_count/total:.1f}%)")
    print(f"  confident   (>=.6): {bands['confident']:>7d} ({100*bands['confident']/total:.1f}%)")
    print(f"  possible (.3-.6):   {bands['possible']:>7d} ({100*bands['possible']/total:.1f}%)")
    print(f"  false_pos    (<.3): {bands['false_positive']:>7d} ({100*bands['false_positive']/total:.1f}%)")
    print()
    print("top 10 bills by false-positive count (likely stale-boilerplate magnets):")
    for bid, cnt in fp_by_bill.most_common(10):
        title, _ = bill_meta.get(bid, ("", ""))
        print(f"  {cnt:>5d}  {bid:<14}  {title[:70]}")
        for client, snippet in fp_examples.get(bid, []):
            print(f"           ↳ {client[:40]:<40}  {snippet}")
    return 0


def cmd_politics_backfill_crec(args, store: Store) -> int:
    """Backfill GovInfo Congressional Record floor-speech metadata to a sidecar DB.

    Walks day-by-day from --start-date to --end-date (default today). Metadata
    only — no full text. Writes to sidecar so the live web DB is untouched.
    Merge later via `politics merge-backfill`.
    """
    import asyncio
    from datetime import date as _date, timedelta
    from pathlib import Path

    store.close()  # don't hold the main DB open

    backfill_path = Path(args.batch_db)
    backfill_path.parent.mkdir(parents=True, exist_ok=True)

    bf_store = Store(db_path=backfill_path)
    if args.reset_cursor:
        bf_store.conn.execute("DELETE FROM adapter_cursor WHERE source = 'govinfo_crec'")

    try:
        start_d = _date.fromisoformat(args.start_date)
    except ValueError:
        print(f"bad --start-date: {args.start_date}", file=sys.stderr)
        return 2
    end_d = _date.fromisoformat(args.end_date) if args.end_date else _date.today()

    existing_cursor = bf_store.get_cursor("govinfo_crec")
    if existing_cursor is None or args.reset_cursor:
        bf_store.set_cursor("govinfo_crec", (start_d - timedelta(days=1)).isoformat())

    from conductor.adapters import registry
    from conductor.http import HttpClient

    cls = registry.get("govinfo_crec")
    batch = max(1, args.batch_days)
    concurrency = max(1, args.concurrency)

    async def _one_round(target_end: _date) -> int:
        async with HttpClient() as http:
            adapter = cls(bf_store, http)
            events = []
            async for ev in adapter.pull(  # type: ignore[call-arg]
                days=batch, end_date=target_end, concurrency=concurrency
            ):
                events.append(ev)
            return bf_store.insert_events(events)

    print(f"backfill-crec -> {backfill_path}  range={start_d}..{end_d} batch={batch} concurrency={concurrency}")
    total_new = 0
    rounds = 0
    while True:
        cur_raw = bf_store.get_cursor("govinfo_crec")
        cur_d = _date.fromisoformat(cur_raw) if cur_raw else (start_d - timedelta(days=1))
        if cur_d >= end_d:
            print("reached end_date")
            break
        try:
            n = asyncio.run(_one_round(end_d))
        except Exception as e:
            print(f"round error: {e}", file=sys.stderr)
            break
        rounds += 1
        total_new += n
        new_cur = bf_store.get_cursor("govinfo_crec")
        print(f"round {rounds}: cursor->{new_cur} +{n} events (cum {total_new})", flush=True)
        if new_cur == cur_raw:
            print("cursor did not advance — stopping")
            break

    bf_store.close()
    print(f"done — {rounds} rounds, {total_new} new events")
    return 0


def cmd_politics_bulk_bills(args, store: Store) -> int:
    """Bulk-load BILLSTATUS XML from govinfo.gov — bypasses api.congress.gov.
    Single HTTP per bill, parallelizable, no key, Cloudflare-cached.
    """
    import asyncio
    from conductor.politics import bulk_billstatus as bb

    bill_types = (
        bb.ALL_BILL_TYPES
        if args.bill_types == "all"
        else tuple(s.strip().lower() for s in args.bill_types.split(",") if s.strip())
    )
    results = asyncio.run(bb.bulk_load(
        store, congress=args.congress, bill_types=bill_types, concurrency=args.concurrency,
    ))
    print(f"bulk load complete (congress {args.congress})")
    total_b = total_e = 0
    for bt, counts in results.items():
        print(f"  {bt:8s} bills={counts['bills_upserted']:>5d}  events={counts['events_inserted']:>6d}  missing={counts['missing']}")
        total_b += counts["bills_upserted"]
        total_e += counts["events_inserted"]
    print(f"  TOTAL    bills={total_b}  events={total_e}")
    return 0


def cmd_politics_daily_update(args, store: Store) -> int:
    """Run every daily-cadence adapter, publishing via snapshot-swap.

    Pattern:
      1. Close the inherited Store handle (we will not write to main DB).
      2. Copy `main.duckdb` → `main.duckdb.staging` (file-level, no lock).
      3. Open Store on staging; run all adapters there.
      4. On success, atomically rename staging → main. Web's per-request
         connections see the new file on next open. No 503 window.

    Failure modes:
      - Crash mid-run: staging file orphaned; cleaned at next run start.
        Cursors did not advance on main, so adapters resume cleanly.
      - Adapter error: caught per-adapter (existing behavior); snapshot
        publishes regardless so partial progress is preserved. Tomorrow's
        run picks up where this one stopped.

    Why not just write to main: web reads + adapter writes + analytics
    middleware writes contend on DuckDB's per-file write lock. Snapshot
    keeps writers off main entirely.
    """
    import os
    import shutil
    from pathlib import Path as _Path

    main_path = _Path(store.db_path).resolve()
    store.close()  # release inherited R/W handle on main; we work on staging

    if not main_path.exists():
        # No main yet (fresh deploy with empty volume) — write directly.
        # Once main exists, subsequent runs use snapshot path.
        print(f"[daily] main DB missing at {main_path}; writing in-place", flush=True)
        run_store = Store(main_path)
        try:
            return _run_daily_update(args, run_store)
        finally:
            run_store.close()

    staging_path = main_path.with_name(main_path.name + ".staging")
    staging_wal = main_path.with_name(staging_path.name + ".wal")
    main_wal = main_path.with_name(main_path.name + ".wal")

    # Drop any stale staging from a prior crashed run
    for p in (staging_path, staging_wal):
        if p.exists():
            try:
                p.unlink()
            except OSError as e:
                print(f"[daily] could not remove stale {p.name}: {e}", file=sys.stderr, flush=True)

    print(f"[daily] snapshot: copying {main_path.name} -> {staging_path.name}", flush=True)
    shutil.copy2(main_path, staging_path)
    if main_wal.exists():
        # Carry forward any uncheckpointed WAL state
        shutil.copy2(main_wal, staging_wal)

    staging_store = Store(staging_path)
    try:
        rc = _run_daily_update(args, staging_store)
    finally:
        staging_store.close()

    print(f"[daily] publishing snapshot -> {main_path.name}", flush=True)
    os.replace(staging_path, main_path)
    if staging_wal.exists():
        os.replace(staging_wal, main_wal)
    elif main_wal.exists():
        # Main's old WAL is no longer consistent with the new file
        try:
            main_wal.unlink()
        except OSError:
            pass
    return rc


def _run_daily_update(args, store: Store) -> int:
    """Run every daily-cadence adapter against `store`."""
    import asyncio
    from conductor.adapters import registry
    from conductor.http import HttpClient

    # Order matters: legislators + committees first (entity refresh), then
    # event adapters that depend on the entity table.
    weekday = __import__("datetime").datetime.now().weekday()  # 0 = Mon

    # Once-a-week refreshes: cheap, but no need daily
    if args.full or weekday == 0:
        from conductor.politics import legislators_sync as ls
        from conductor.politics import committees_sync as cs
        from conductor.politics import legislators_social_sync as lss
        from conductor.politics import pvi_sync as ps
        ls.sync(store)
        cs.sync(store)
        try:
            n_pvi = ps.sync(store)
            print(f"[daily] pvi_sync: {n_pvi} rows", flush=True)
        except Exception as e:
            print(f"[daily] pvi_sync: ERROR {e}", file=sys.stderr, flush=True)
        try:
            n_social = lss.sync(store)
            print(f"[daily] legislators-social-sync: {n_social} rows", flush=True)
        except Exception as e:
            print(f"[daily] legislators-social-sync: ERROR {e}", file=sys.stderr, flush=True)
        # Funding totals — FEC files quarterly, weekly refresh is plenty.
        # Throttled (~65 req/min) to stay under OpenFEC's 1000/hr cap.
        # skip_if_present=True so we only fetch (bioguide, cycle) pairs we don't have yet.
        try:
            from conductor.politics import funding_sync as fs
            import asyncio as _asyncio
            _asyncio.run(fs.sync(store, cycles=(2026, 2024, 2022, 2020)))
            print("[daily] funding_sync: complete", flush=True)
        except Exception as e:
            print(f"[daily] funding_sync: ERROR {e}", file=sys.stderr, flush=True)

    daily_adapters = [
        "congress_rollcalls",         # House — fast, no key
        "senate_rollcalls",           # Senate — fast, no key
        "congress_amendments",        # api.congress.gov, requires key
        "congress_bill_actions",      # depends on bills already in DB
        "govinfo_crec",               # floor speeches, no key
        "govinfo_bill_text",          # bill text → FS (incremental, cursor-aware)
        "congress_bill_summaries",    # CRS summaries (skip-if-current SQL filter)
    ]
    if args.with_bills:
        daily_adapters.insert(2, "congress_bills")  # api-key, slow
    if args.with_lda:
        daily_adapters.append("lda_senate")          # very slow without key

    summary: list[str] = []
    async def _run():
        async with HttpClient() as http:
            for name in daily_adapters:
                try:
                    cls = registry.get(name)
                    adapter = cls(store, http)
                    n = await adapter.run_pull()
                    summary.append(f"{name}: +{n}")
                    print(f"[daily] {name}: +{n} new events", flush=True)
                except Exception as e:
                    summary.append(f"{name}: ERROR {type(e).__name__}")
                    print(f"[daily] {name}: ERROR {e}", file=sys.stderr, flush=True)

    asyncio.run(_run())

    # Ingest any new bill_text from the FS tree into the DB. Cheap when the
    # govinfo_bill_text pull above found 0 new packages (no-op walk).
    try:
        from conductor.politics import bill_text as bt_mod
        inserted, skipped = bt_mod.ingest_from_fs(store, bt_mod.DEFAULT_TEXT_ROOT)
        summary.append(f"ingest-bill-text: +{inserted}/skip {skipped}")
        print(f"[daily] ingest-bill-text: inserted={inserted} skipped={skipped}", flush=True)
    except Exception as e:
        summary.append(f"ingest-bill-text: ERROR {type(e).__name__}")
        print(f"[daily] ingest-bill-text: ERROR {e}", file=sys.stderr, flush=True)

    print("[daily] done — " + " · ".join(summary))
    return 0


def cmd_politics_sync_committees(args, store: Store) -> int:
    from conductor.politics import committees_sync as cs
    n = cs.sync(store)
    print(f"committee assignments synced: {n} rows")
    return 0


def cmd_politics_sync_pvi(args, store: Store) -> int:
    from conductor.politics import pvi_sync as ps
    n = ps.sync(store, congress=args.congress)
    print(f"district_pvi rows synced: {n} (congress {args.congress})")
    return 0


def cmd_politics_sync_social(args, store: Store) -> int:
    from conductor.politics import legislators_social_sync as lss
    n = lss.sync(store)
    print(f"legislator_social rows: {n}")
    return 0


def cmd_politics_sync_funding(args, store: Store) -> int:
    import asyncio
    from conductor.politics import funding_sync as fs
    cycles = tuple(int(c) for c in args.cycles.split(",") if c.strip())
    counts = asyncio.run(fs.sync(store, cycles=cycles))
    print(f"funding totals synced — {counts}")
    return 0


def cmd_politics_ingest_bill_text(args, store: Store) -> int:
    from conductor.politics import bill_text as bill_text_mod
    root = args.root or bill_text_mod.DEFAULT_TEXT_ROOT
    inserted, skipped = bill_text_mod.ingest_from_fs(store, root, replace=args.replace)
    print(f"ingest-bill-text: inserted={inserted} skipped={skipped} root={root}")
    return 0


def cmd_politics_summarize_passed(args, store: Store) -> int:
    """Generate AI delta summaries for bills that became law (or were enrolled).

    Use --dry-run to print the cost estimate without making any LLM calls.
    """
    from conductor.politics import bill_summary as bs_mod
    from conductor.politics import bill_text as bt_mod

    bs_mod.ensure_schema(store)
    plan = bs_mod.find_passed_bills(store)
    if args.bill_id:
        plan = [p for p in plan if p["bill_id"] == args.bill_id]
    if args.limit:
        plan = plan[: args.limit]
    summary = bs_mod.estimate_batch_cost(plan)
    print(f"plan: {summary['count']} bills (punt={summary['count_punt']}), "
          f"estimated total ${summary['total_usd']:.2f}")
    for tier, usd in sorted(summary["by_tier"].items()):
        print(f"  tier {tier}: ${usd:.2f}")
    for model, usd in sorted(summary["by_model"].items()):
        print(f"  model {model}: ${usd:.2f}")

    if args.dry_run:
        for p in plan[:20]:
            print(f"  {p['bill_id']}  {p['from_code']}→{p['to_code']}  "
                  f"{p['combined_tokens']//1000}k tok  {p['tier']}  ${p['est_usd']:.4f}")
        if len(plan) > 20:
            print(f"  ... +{len(plan) - 20} more")
        return 0

    # Generate
    actual_total = 0.0
    for i, p in enumerate(plan, 1):
        bill_id = p["bill_id"]
        from_code, to_code = p["from_code"], p["to_code"]
        from_body = bt_mod.get_body_db(store, bill_id, from_code) or ""
        to_body = bt_mod.get_body_db(store, bill_id, to_code) or ""
        if not (from_body and to_body):
            print(f"  skip {bill_id}: missing body")
            continue
        from_label = bt_mod.STAGE_LABELS.get(from_code, from_code)
        to_label = bt_mod.STAGE_LABELS.get(to_code, to_code)
        print(f"[{i}/{len(plan)}] {bill_id} {from_code}→{to_code} "
              f"({p['tier']}, est ${p['est_usd']:.4f})... ", end="", flush=True)
        try:
            res = bs_mod.summarize_delta(
                store, bill_id, from_code, to_code,
                from_label, to_label, from_body, to_body,
                force=args.force,
            )
        except Exception as e:
            print(f"ERR: {e}")
            continue
        if res is None:
            print("skipped (no API key)")
            continue
        # actual cost = (in_tokens * in_rate + out_tokens * out_rate) / 1M
        in_rate, out_rate = bs_mod._PRICING.get(
            bs_mod.HAIKU_MODEL if "haiku" in (res.tier or "") else bs_mod.SONNET_MODEL,
            (3.0, 15.0),
        )
        # better: lookup by model in cache row — quick approximation here
        actual = (res.input_tokens * in_rate + res.output_tokens * out_rate) / 1_000_000
        actual_total += actual
        print(f"OK ({res.input_tokens} in, {res.output_tokens} out, ~${actual:.4f})")
    print(f"DONE — actual total ~${actual_total:.2f}")
    return 0


def cmd_politics_web(args, store: Store) -> int:
    import uvicorn
    from conductor.politics.web.app import create_app
    store.close()  # uvicorn workers will open their own
    app = create_app(db_path=args.db)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="conductor")
    parser.add_argument("--db", type=Path, default=Path("data/conductor.duckdb"))
    parser.add_argument("--verbose", "-v", action="store_true")

    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="list registered adapters")
    p_list.set_defaults(func=cmd_list)

    p_pull = sub.add_parser("pull", help="run an adapter's pull()")
    p_pull.add_argument("adapter")
    p_pull.set_defaults(func=cmd_pull)

    # politics — vertical-specific surface
    p_politics = sub.add_parser("politics", help="politics vertical commands")
    psub = p_politics.add_subparsers(dest="politics_cmd", required=True)

    pp_sync = psub.add_parser("sync-legislators", help="sync legislators from @unitedstates/congress-legislators")
    pp_sync.set_defaults(func=cmd_politics_sync)

    pp_grid = psub.add_parser("grid", help="render ANSI activity grid for a legislator")
    pp_grid.add_argument("bioguide")
    pp_grid.add_argument("--days", type=int, default=180)
    pp_grid.set_defaults(func=cmd_politics_grid)

    import os as _os
    _default_port = int(_os.environ.get("PORT", 8770))
    pp_web = psub.add_parser("web", help="run FastAPI web app")
    pp_web.add_argument("--host", default=_os.environ.get("HOST", "127.0.0.1"))
    pp_web.add_argument("--port", type=int, default=_default_port)
    pp_web.set_defaults(func=cmd_politics_web)

    pp_bf = psub.add_parser(
        "backfill-bills",
        help="loop bills adapter into a sidecar DB (run alongside the web app)",
    )
    pp_bf.add_argument("--rounds", type=int, default=30, help="max pulls before stopping")
    pp_bf.add_argument("--batch-db", default="data/backfill.duckdb",
                       help="sidecar DB path (don't write into the main DB)")
    pp_bf.add_argument("--reset-cursor", action="store_true",
                       help="clear cursor so the run starts from scratch")
    pp_bf.set_defaults(func=cmd_politics_backfill_bills)

    pp_bflda = psub.add_parser(
        "backfill-lda",
        help="backfill Senate LDA filings for one Congress into a sidecar DB",
    )
    pp_bflda.add_argument("--congress", type=int, required=True)
    pp_bflda.add_argument("--batch-db", default="data/lda.duckdb",
                          help="sidecar DB path (don't write into the main DB)")
    pp_bflda.add_argument("--reset-cursor", action="store_true",
                          help="clear per-period cursors so the run starts from scratch")
    pp_bflda.set_defaults(func=cmd_politics_backfill_lda)

    pp_mg = psub.add_parser(
        "merge-backfill",
        help="merge a sidecar backfill DB into the main DB",
    )
    pp_mg.add_argument("--batch-db", default="data/backfill.duckdb")
    pp_mg.add_argument(
        "--lda-events-only",
        action="store_true",
        help="for LDA sidecars: skip bulky `lda_filing` events; copy only `bill_lobbied` "
             "events + the lda_filings entity table (saves ~150-200MB on main).",
    )
    pp_mg.set_defaults(func=cmd_politics_merge_backfill)

    pp_bulk = psub.add_parser(
        "bulk-bills",
        help="bulk-load BILLSTATUS XML from govinfo.gov (no API key, parallel, fast)",
    )
    pp_bulk.add_argument("--congress", type=int, required=True)
    pp_bulk.add_argument("--bill-types", default="all",
                         help="comma-list (hr,s,hjres,sjres,hconres,sconres,hres,sres) or 'all'")
    pp_bulk.add_argument("--concurrency", type=int, default=24)
    pp_bulk.set_defaults(func=cmd_politics_bulk_bills)

    pp_lv = psub.add_parser(
        "lobby-validate",
        help="score bill_lobbied events vs bill metadata; report false-positive rate",
    )
    pp_lv.set_defaults(func=cmd_politics_lobby_validate)

    pp_bfcrec = psub.add_parser(
        "backfill-crec",
        help="backfill GovInfo Congressional Record floor speeches into a sidecar DB",
    )
    pp_bfcrec.add_argument("--start-date", default="2025-01-03",
                           help="ISO date (cold-start = 119th opening 2025-01-03)")
    pp_bfcrec.add_argument("--end-date", default=None,
                           help="ISO date (default: today)")
    pp_bfcrec.add_argument("--batch-db", default="data/crec.duckdb",
                           help="sidecar DB path (don't write into the main DB)")
    pp_bfcrec.add_argument("--batch-days", type=int, default=14,
                           help="days walked per round")
    pp_bfcrec.add_argument("--concurrency", type=int, default=8,
                           help="parallel day fetches (cap at 8)")
    pp_bfcrec.add_argument("--reset-cursor", action="store_true",
                           help="clear cursor so the run starts from --start-date")
    pp_bfcrec.set_defaults(func=cmd_politics_backfill_crec)

    pp_du = psub.add_parser(
        "daily-update",
        help="run all daily-cadence adapters (Railway cron entry point)",
    )
    pp_du.add_argument("--full", action="store_true",
                       help="also re-sync legislators + committees (default: weekly on Monday)")
    pp_du.add_argument("--with-bills", action="store_true",
                       help="include api.congress.gov bills pull (slow, requires key)")
    pp_du.add_argument("--with-lda", action="store_true",
                       help="include LDA pull (very slow without LDA_API_KEY)")
    pp_du.set_defaults(func=cmd_politics_daily_update)

    pp_sf = psub.add_parser(
        "sync-funding",
        help="sync per-legislator funding totals from OpenFEC",
    )
    pp_sf.add_argument("--cycles", default="2026,2024",
                       help="comma-list of election cycles (year=even). default: 2026,2024")
    pp_sf.set_defaults(func=cmd_politics_sync_funding)

    pp_sum = psub.add_parser(
        "summarize-passed",
        help="generate AI delta summaries for bills that became law (or were enrolled)",
    )
    pp_sum.add_argument("--dry-run", action="store_true",
                        help="print cost estimate without making LLM calls")
    pp_sum.add_argument("--limit", type=int, default=None,
                        help="cap to first N bills")
    pp_sum.add_argument("--bill-id", default=None,
                        help='only this bill (e.g. "119:hr:1968")')
    pp_sum.add_argument("--force", action="store_true",
                        help="regenerate even if cached")
    pp_sum.set_defaults(func=cmd_politics_summarize_passed)

    pp_ibt = psub.add_parser(
        "ingest-bill-text",
        help="load bill text from data/bill_text/ filesystem tree into the bill_text DB table",
    )
    pp_ibt.add_argument("--root", type=Path, default=None,
                        help="override text root (default: $TALLYHQ_TEXT_ROOT or data/bill_text)")
    pp_ibt.add_argument("--replace", action="store_true",
                        help="replace existing rows even if sha256 matches")
    pp_ibt.set_defaults(func=cmd_politics_ingest_bill_text)

    pp_sc = psub.add_parser(
        "sync-committees",
        help="sync committee assignments for every legislator",
    )
    pp_sc.add_argument("--all-terms", action="store_true",
                       help="include past committee assignments, not just current")
    pp_sc.add_argument("--concurrency", type=int, default=8)
    pp_sc.set_defaults(func=cmd_politics_sync_committees)

    pp_pvi = psub.add_parser(
        "sync-pvi",
        help="sync Cook PVI per district + state from Wikipedia",
    )
    pp_pvi.add_argument("--congress", type=int, default=119,
                        help="Congress number to associate rows with (default: 119)")
    pp_pvi.set_defaults(func=cmd_politics_sync_pvi)

    pp_ss = psub.add_parser(
        "sync-legislators-social",
        help="sync social-media handles from @unitedstates/congress-legislators",
    )
    pp_ss.set_defaults(func=cmd_politics_sync_social)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows console defaults to cp1252; ensure unicode block chars print
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    except (AttributeError, OSError):
        pass

    from conductor.secrets import load_dotenv
    load_dotenv()

    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # httpx logs full request URLs at INFO; suppress to avoid leaking
    # api_key query params (api.congress.gov) into logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    db_path = _resolve_db_path(args)
    store = Store(db_path=db_path)
    try:
        return args.func(args, store)
    finally:
        store.close()


def _resolve_db_path(args) -> Path:
    """Return the DB path to open. For pull commands targeting an adapter that
    declares `requires_db = False`, return an in-memory DuckDB path so we
    never contend for the real file lock.
    """
    requested = getattr(args, "db", None) or Path("data/conductor.duckdb")
    func = getattr(args, "func", None)
    if func is cmd_pull:
        try:
            cls = registry.get(args.adapter)
        except KeyError:
            return requested
        if not getattr(cls, "requires_db", True):
            return Path(":memory:")
    return requested


if __name__ == "__main__":
    raise SystemExit(main())
