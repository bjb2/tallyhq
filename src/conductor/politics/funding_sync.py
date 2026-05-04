"""Sync per-legislator funding totals from OpenFEC.

Iterates the legislators table, picks each member's FEC ID, queries
OpenFEC `/candidate/{id}/totals/?cycle={cycle}`, upserts a row in
funding_totals. Concurrent fetches via asyncio + httpx.

This is a sync, not an event stream — like committees_sync. Run as a
weekly cadence (FEC filings come quarterly).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date as _date
from typing import Optional

import httpx

from conductor.politics import entities as ent_mod
from conductor.politics import funding as fm
from conductor.secrets import require
from conductor.store import Store

logger = logging.getLogger(__name__)

API_BASE = "https://api.open.fec.gov/v1"


def _resolve_fec_id(ids_json: str | dict | None) -> Optional[str]:
    """Members store fec as either a string or list of historical IDs.
    Pick the most recent (last) — best proxy for current campaign committee."""
    if not ids_json:
        return None
    ids = ids_json if isinstance(ids_json, dict) else json.loads(ids_json)
    fec = ids.get("fec")
    if not fec:
        return None
    if isinstance(fec, list):
        return fec[-1] if fec else None
    return str(fec)


def _date_or_none(s: Optional[str]) -> Optional[_date]:
    if not s:
        return None
    try:
        return _date.fromisoformat(s[:10])
    except ValueError:
        return None


async def _fetch_totals(
    client: httpx.AsyncClient, api_key: str, fec_id: str, cycle: int,
) -> Optional[dict]:
    url = f"{API_BASE}/candidate/{fec_id}/totals/"
    try:
        r = await client.get(url, params={"cycle": cycle, "api_key": api_key})
        if r.status_code != 200:
            return None
        d = r.json()
        results = d.get("results") or []
        return results[0] if results else None
    except (httpx.HTTPError, ValueError) as e:
        logger.debug("openfec %s cycle %s failed: %s", fec_id, cycle, e)
        return None


async def _search_candidate(
    client: httpx.AsyncClient,
    api_key: str,
    *,
    name: str,
    cycle: int,
    chamber: str,
    state: str,
    district: Optional[int],
) -> Optional[str]:
    """Resolve fec candidate_id by name when stored ID is stale/missing.

    Filters by office (H/S), state, and (for House) district. Returns the
    highest-relevance match's candidate_id, or None.
    """
    office = "H" if chamber == "house" else "S" if chamber == "senate" else None
    if not office:
        return None
    params = {
        "q": name,
        "cycle": cycle,
        "office": office,
        "state": state,
        "api_key": api_key,
        "per_page": 5,
        "sort": "-receipts",
    }
    if office == "H" and district is not None:
        params["district"] = f"{int(district):02d}"
    try:
        r = await client.get(f"{API_BASE}/candidates/search/", params=params)
        if r.status_code != 200:
            return None
        results = r.json().get("results") or []
    except (httpx.HTTPError, ValueError) as e:
        logger.debug("openfec search %s cycle %s failed: %s", name, cycle, e)
        return None

    last_token = (name.split()[-1] if name else "").lower()
    for cand in results:
        if office and cand.get("office") != office:
            continue
        if cand.get("state") and cand["state"] != state:
            continue
        cycles = cand.get("cycles") or []
        if cycle not in cycles:
            continue
        cand_name = (cand.get("name") or "").lower()
        if last_token and last_token not in cand_name:
            continue
        cid = cand.get("candidate_id")
        if cid:
            return cid
    return None


async def sync(
    store: Store,
    cycles: tuple[int, ...] = (2026, 2024),
    concurrency: int = 4,
    sleep_per_call: float = 3.7,
    skip_if_present: bool = True,
) -> dict[str, int]:
    """Pull totals for every legislator across each requested cycle.

    Default cycles: current 2026 + most-recently-completed 2024.
    Returns counters {cycle -> rows_written}.

    Throttle: OpenFEC personal-key cap is 1000/hour (~16 req/min). Defaults
    keep us well under: 4 concurrent × 3.7s/call ≈ 65 req/min. Set
    sleep_per_call=0 once you have an upgraded FEC key.

    skip_if_present: don't re-fetch (bioguide, cycle) rows already in DB.
    """
    fm.ensure_schema(store)
    api_key = require("OPENFEC_API_KEY")
    members = ent_mod.list_all(store, active_only=True)
    counts: dict[int, int] = {c: 0 for c in cycles}
    sem = asyncio.Semaphore(concurrency)

    # Existing rows we can skip (resumability + saves API quota)
    existing: set[tuple[str, int]] = set()
    if skip_if_present:
        for r in store.conn.execute(
            "SELECT bioguide_id, cycle FROM funding_totals"
        ).fetchall():
            existing.add((r[0], int(r[1])))

    async def _one(client, ent, cycle):
        if (ent.bioguide_id, cycle) in existing:
            return

        primary_id = None
        if ent.ids and ent.ids.get("fec"):
            v = ent.ids["fec"]
            primary_id = v[-1] if isinstance(v, list) and v else (v if isinstance(v, str) else None)

        # 1. cached resolution (positive or negative)
        cached, cached_id = fm.get_resolution(store, ent.bioguide_id, cycle)
        if cached:
            if cached_id is None:
                return  # negative cache — name search already failed
            fec_id = cached_id
            data = None
            async with sem:
                data = await _fetch_totals(client, api_key, fec_id, cycle)
                if sleep_per_call > 0:
                    await asyncio.sleep(sleep_per_call)
            if not data:
                return
        else:
            # 2. primary path
            fec_id = primary_id
            data = None
            if fec_id:
                async with sem:
                    data = await _fetch_totals(client, api_key, fec_id, cycle)
                    if sleep_per_call > 0:
                        await asyncio.sleep(sleep_per_call)
            # 3. fallback: name search → /totals/
            if not data:
                async with sem:
                    resolved = await _search_candidate(
                        client, api_key,
                        name=ent.full_name or f"{ent.first_name} {ent.last_name}".strip(),
                        cycle=cycle,
                        chamber=ent.chamber,
                        state=ent.state,
                        district=ent.district,
                    )
                    if sleep_per_call > 0:
                        await asyncio.sleep(sleep_per_call)
                if resolved and resolved != primary_id:
                    fec_id = resolved
                    async with sem:
                        data = await _fetch_totals(client, api_key, fec_id, cycle)
                        if sleep_per_call > 0:
                            await asyncio.sleep(sleep_per_call)
                    fm.put_resolution(store, ent.bioguide_id, cycle, resolved, "search")
                else:
                    fm.put_resolution(store, ent.bioguide_id, cycle, None, "search")
                    return
        if not data:
            return
        t = fm.FundingTotal(
            bioguide_id=ent.bioguide_id,
            cycle=cycle,
            fec_id=fec_id,
            receipts=float(data.get("receipts") or 0),
            disbursements=float(data.get("disbursements") or 0),
            cash_on_hand=(
                float(data.get("last_cash_on_hand_end_period"))
                if data.get("last_cash_on_hand_end_period") is not None
                else (float(data.get("cash_on_hand_end_period"))
                      if data.get("cash_on_hand_end_period") is not None else None)
            ),
            debts=float(data.get("debts_owed_by_committee")) if data.get("debts_owed_by_committee") is not None else None,
            individual_contributions=float(data.get("individual_contributions")) if data.get("individual_contributions") is not None else None,
            pac_contributions=float(data.get("other_political_committee_contributions")) if data.get("other_political_committee_contributions") is not None else None,
            party_contributions=float(data.get("political_party_committee_contributions")) if data.get("political_party_committee_contributions") is not None else None,
            candidate_contribution=float(data.get("candidate_contribution")) if data.get("candidate_contribution") is not None else None,
            coverage_start=_date_or_none(data.get("coverage_start_date")),
            coverage_end=_date_or_none(data.get("coverage_end_date")),
            last_report_label=data.get("last_report_type_full"),
        )
        fm.upsert(store, t)
        counts[cycle] += 1

    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        tasks = []
        for ent in members:
            for cycle in cycles:
                tasks.append(_one(client, ent, cycle))
        await asyncio.gather(*tasks)

    logger.info("funding sync — %s rows written across cycles %s", counts, cycles)
    return {str(k): v for k, v in counts.items()}
