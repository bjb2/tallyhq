"""Read-side helpers for LDA / lobbying surfaces.

bill_lobbied events carry: client_id, client_name, registrant_id, registrant_name,
filing_year, filing_period, amount_share, issue_codes_for_bill, bill_id.

These views aggregate them for the bill detail page, the client profile page,
and the landing "Most lobbied bills" card.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from conductor.politics import lobby_match
from conductor.store import Store


@dataclass
class ClientLobbyOnBill:
    client_id: str
    client_name: str
    filings: int
    total_amount: float
    issue_codes: list[str]
    latest_period: str   # "2026 Q1"
    match_confidence: float = 0.0   # max score across this client's filings on this bill
    match_band: str = "false_positive"  # "confident" | "possible" | "false_positive"


@dataclass
class LobbiedBill:
    bill_id: str
    title: str
    congress: int
    bill_type: str
    number: int
    cosponsor_count: int
    sponsor_bioguide: Optional[str]
    filings_count: int
    distinct_clients: int


def top_clients_for_bill(
    store: Store,
    bill_id: str,
    limit: int = 10,
    min_confidence: float = lobby_match.THRESHOLD_BILL_PAGE_POSSIBLE,
) -> list[ClientLobbyOnBill]:
    """For a bill, list top clients lobbying on it, ranked by filing count.

    Computes a match-confidence score per client (max across their
    filings) using `lobby_match.score_match`. Filings whose extracted
    bill ref likely points at the wrong bill (e.g. stale boilerplate
    from a previous Congress) are filtered out by `min_confidence`.
    Pass `min_confidence=0.0` to disable filtering.
    """
    # Load the bill once for scoring (title + policy_area).
    from conductor.politics import bills as bills_mod
    bill = bills_mod.get(store, bill_id)
    bill_title = bill.title if bill else ""
    bill_policy = bill.policy_area if bill else ""

    rows = store.conn.execute(
        """
        SELECT
          json_extract_string(payload, '$.client_id')   AS client_id,
          json_extract_string(payload, '$.client_name') AS client_name,
          COUNT(*)                                      AS filings,
          SUM(CAST(json_extract_string(payload, '$.amount_share') AS DOUBLE)) AS total_amount,
          MAX(CAST(json_extract_string(payload, '$.filing_year') AS INTEGER)) AS latest_year,
          MAX(json_extract_string(payload, '$.filing_period')) AS latest_period
        FROM events
        WHERE event_type = 'bill_lobbied'
          AND json_extract_string(payload, '$.bill_id') = ?
        GROUP BY client_id, client_name
        ORDER BY filings DESC, total_amount DESC NULLS LAST
        """,
        [bill_id],
    ).fetchall()
    out: list[ClientLobbyOnBill] = []
    for r in rows:
        if not r[0]:
            continue
        # Pull issue codes + mention text per filing so we can score each
        # and take the max. (A client may have one good filing and 5 stale
        # ones — the good one establishes legitimate interest in the bill.)
        ic_rows = store.conn.execute(
            """
            SELECT json_extract_string(payload, '$.issue_codes_for_bill'),
                   json_extract_string(payload, '$.mention_text')
            FROM events
            WHERE event_type = 'bill_lobbied'
              AND json_extract_string(payload, '$.bill_id') = ?
              AND json_extract_string(payload, '$.client_id') = ?
            LIMIT 50
            """,
            [bill_id, r[0]],
        ).fetchall()
        codes_set: set[str] = set()
        best_score = 0.0
        best_band = "false_positive"
        for ic, mention in ic_rows:
            codes: list[str] = []
            try:
                codes = json.loads(ic) if ic else []
            except (TypeError, ValueError):
                codes = []
            for code in codes:
                codes_set.add(code)
            ms = lobby_match.score_match(
                bill_title=bill_title,
                bill_policy_area=bill_policy,
                issue_codes=codes,
                mention_text=mention or "",
            )
            if ms.score > best_score:
                best_score = ms.score
                best_band = ms.band

        if best_score < min_confidence:
            continue

        period_label = ""
        if r[4] and r[5]:
            qmap = {
                "first_quarter": "Q1", "second_quarter": "Q2",
                "third_quarter": "Q3", "fourth_quarter": "Q4",
            }
            period_label = f"{r[4]} {qmap.get(r[5], r[5])}"
        out.append(ClientLobbyOnBill(
            client_id=r[0],
            client_name=r[1] or r[0],
            filings=int(r[2] or 0),
            total_amount=float(r[3] or 0),
            issue_codes=sorted(codes_set)[:6],
            latest_period=period_label,
            match_confidence=best_score,
            match_band=best_band,
        ))
        if len(out) >= limit:
            break
    return out


def most_lobbied_bills(store: Store, limit: int = 8) -> list[LobbiedBill]:
    """Bills with the highest count of bill_lobbied filings, joined to bills entity."""
    rows = store.conn.execute(
        f"""
        WITH agg AS (
          SELECT
            json_extract_string(payload, '$.bill_id') AS bill_id,
            COUNT(*) AS filings,
            COUNT(DISTINCT json_extract_string(payload, '$.client_id')) AS distinct_clients
          FROM events
          WHERE event_type = 'bill_lobbied'
          GROUP BY bill_id
          ORDER BY filings DESC
          LIMIT {int(limit) * 4}
        )
        SELECT a.bill_id, b.title, b.congress, b.bill_type, b.number,
               b.cosponsor_count, b.sponsor_bioguide, a.filings, a.distinct_clients
        FROM agg a
        LEFT JOIN bills b ON b.bill_id = a.bill_id
        WHERE b.bill_id IS NOT NULL
        ORDER BY a.filings DESC
        LIMIT {int(limit)}
        """
    ).fetchall()
    return [
        LobbiedBill(
            bill_id=r[0],
            title=r[1] or "",
            congress=int(r[2] or 0),
            bill_type=r[3] or "",
            number=int(r[4] or 0),
            cosponsor_count=int(r[5] or 0),
            sponsor_bioguide=r[6],
            filings_count=int(r[7] or 0),
            distinct_clients=int(r[8] or 0),
        )
        for r in rows
    ]


@dataclass
class ClientProfile:
    client_id: str
    name: str
    total_filings: int
    distinct_registrants: int
    distinct_bills: int
    total_income: float
    distinct_periods: int


def get_client(store: Store, client_id: str) -> Optional[ClientProfile]:
    row = store.conn.execute(
        """
        SELECT
          ANY_VALUE(client_name) AS client_disp_name,
          COUNT(*) AS filings,
          COUNT(DISTINCT registrant_id) AS registrants,
          COALESCE(SUM(income), 0) + COALESCE(SUM(expenses), 0) AS total_income,
          COUNT(DISTINCT (filing_year || ':' || filing_period)) AS periods
        FROM lda_filings
        WHERE client_id = ?
        GROUP BY client_id
        """,
        [client_id],
    ).fetchone()
    if not row or not row[0]:
        return None
    bills = store.conn.execute(
        """
        SELECT COUNT(DISTINCT json_extract_string(payload, '$.bill_id'))
        FROM events
        WHERE event_type = 'bill_lobbied'
          AND json_extract_string(payload, '$.client_id') = ?
        """,
        [client_id],
    ).fetchone()
    return ClientProfile(
        client_id=client_id,
        name=row[0],
        total_filings=int(row[1] or 0),
        distinct_registrants=int(row[2] or 0),
        distinct_bills=int(bills[0] or 0),
        total_income=float(row[3] or 0),
        distinct_periods=int(row[4] or 0),
    )


@dataclass
class ClientBillRow:
    bill_id: str
    title: str
    congress: int
    bill_type: str
    number: int
    filings: int
    issue_codes: list[str]


def bills_for_client(store: Store, client_id: str, limit: int = 50) -> list[ClientBillRow]:
    """Bills this client has lobbied on, ranked by filing count."""
    rows = store.conn.execute(
        f"""
        WITH agg AS (
          SELECT
            json_extract_string(payload, '$.bill_id') AS bill_id,
            COUNT(*) AS filings,
            list(DISTINCT json_extract_string(payload, '$.issue_codes_for_bill')) AS codes_lists
          FROM events
          WHERE event_type = 'bill_lobbied'
            AND json_extract_string(payload, '$.client_id') = ?
          GROUP BY bill_id
        )
        SELECT a.bill_id, b.title, b.congress, b.bill_type, b.number, a.filings, a.codes_lists
        FROM agg a
        LEFT JOIN bills b ON b.bill_id = a.bill_id
        ORDER BY a.filings DESC
        LIMIT {int(limit)}
        """,
        [client_id],
    ).fetchall()
    out = []
    for r in rows:
        codes_set: set[str] = set()
        for cl in (r[6] or []):
            try:
                for code in (json.loads(cl) if cl else []):
                    codes_set.add(code)
            except (TypeError, ValueError):
                pass
        out.append(ClientBillRow(
            bill_id=r[0],
            title=r[1] or "",
            congress=int(r[2] or 0) if r[2] else 0,
            bill_type=r[3] or "",
            number=int(r[4] or 0) if r[4] else 0,
            filings=int(r[5] or 0),
            issue_codes=sorted(codes_set)[:6],
        ))
    return out


@dataclass
class ClientRegistrant:
    registrant_id: str
    name: str
    filings: int


def registrants_for_client(store: Store, client_id: str, limit: int = 20) -> list[ClientRegistrant]:
    rows = store.conn.execute(
        f"""
        SELECT registrant_id, ANY_VALUE(registrant_name) AS reg_name, COUNT(*) AS filings
        FROM lda_filings
        WHERE client_id = ?
        GROUP BY registrant_id
        ORDER BY filings DESC
        LIMIT {int(limit)}
        """,
        [client_id],
    ).fetchall()
    return [ClientRegistrant(registrant_id=r[0], name=r[1] or r[0], filings=int(r[2] or 0))
            for r in rows]
