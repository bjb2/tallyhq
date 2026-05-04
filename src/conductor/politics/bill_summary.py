"""AI-generated bill-version delta summaries via OpenRouter.

Four-tier dispatch based on combined token count:

  ≤ 180k tokens   → Haiku 4.5 single call (cheap default, ~95% of bills)
  ≤ 900k tokens   → Sonnet 4.6 single call w/ 1M-beta context (mega-bills,
                    single coherent narrative — better than chunking)
  ≤ 1.8M tokens   → Sonnet 4.6 chunked (extreme cases — multi-thousand-page
                    omnibus that exceeds even 1M context)
  > 1.8M tokens   → punt: render "Bill too large for auto-summary" stub

All tiers use the SAME prompt template — only model + chunking strategy vary.
This keeps output style consistent regardless of which tier was hit.

All LLM calls go through OpenRouter via the `openai` SDK. Direct Anthropic
SDK use is forbidden by project policy (see memory).

Cache key: sha256 of from-body + sha256 of to-body. Hit on the same pair
forever; regenerate when either text changes.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from conductor.store import Store

logger = logging.getLogger(__name__)

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

# Models per tier — overrideable via env.
HAIKU_MODEL = os.environ.get("TALLYHQ_HAIKU_MODEL", "anthropic/claude-haiku-4.5")
SONNET_MODEL = os.environ.get("TALLYHQ_SONNET_MODEL", "anthropic/claude-sonnet-4.6")

# Token budgets — chars/4 is a fast estimator for English-language bill text.
_CHARS_PER_TOKEN = 4
HAIKU_SINGLE_BUDGET = 180_000      # Haiku 4.5 = 200K context
SONNET_SINGLE_BUDGET = 900_000     # Sonnet 4.6 = 1M beta context, 100K headroom
SONNET_CHUNKED_BUDGET = 1_800_000  # 2x of single, for rare multi-thousand-page bills
CHUNK_TOKEN_TARGET = 700_000       # per-call when chunking on Sonnet 1M

BILL_SUMMARY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS bill_text_summary (
    bill_id        VARCHAR,
    from_code      VARCHAR,
    to_code        VARCHAR,
    sha256_pair    VARCHAR,
    model          VARCHAR,
    tier           VARCHAR,            -- "single" | "chunked" | "punt"
    summary_md     VARCHAR,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    chunk_count    INTEGER,
    generated_at   TIMESTAMPTZ,
    PRIMARY KEY (bill_id, from_code, to_code)
);
CREATE INDEX IF NOT EXISTS idx_bill_text_summary_bill ON bill_text_summary(bill_id);
"""


def ensure_schema(store: "Store") -> None:
    store.conn.execute(BILL_SUMMARY_SCHEMA_SQL)


@dataclass
class SummaryResult:
    summary_md: str
    tier: str
    input_tokens: int
    output_tokens: int
    chunk_count: int


# ---------------------------------------------------------------------------
# Cache layer
# ---------------------------------------------------------------------------

def sha256_pair(from_body: str, to_body: str) -> str:
    h = hashlib.sha256()
    h.update(hashlib.sha256(from_body.encode("utf-8")).digest())
    h.update(hashlib.sha256(to_body.encode("utf-8")).digest())
    return h.hexdigest()


def get_cached(store: "Store", bill_id: str, from_code: str, to_code: str,
               sha_pair: str) -> Optional[dict]:
    ensure_schema(store)
    row = store.conn.execute(
        """
        SELECT summary_md, tier, model, input_tokens, output_tokens,
               chunk_count, generated_at
        FROM bill_text_summary
        WHERE bill_id = ? AND from_code = ? AND to_code = ?
              AND sha256_pair = ?
        """,
        [bill_id, from_code, to_code, sha_pair],
    ).fetchone()
    if not row:
        return None
    return {
        "summary_md": row[0],
        "tier": row[1],
        "model": row[2],
        "input_tokens": int(row[3] or 0),
        "output_tokens": int(row[4] or 0),
        "chunk_count": int(row[5] or 0),
        "generated_at": row[6],
    }


def store_summary(store: "Store", bill_id: str, from_code: str, to_code: str,
                  sha_pair: str, model: str, result: SummaryResult) -> None:
    ensure_schema(store)
    store.conn.execute(
        """
        INSERT INTO bill_text_summary
            (bill_id, from_code, to_code, sha256_pair, model, tier,
             summary_md, input_tokens, output_tokens, chunk_count, generated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (bill_id, from_code, to_code) DO UPDATE SET
            sha256_pair   = excluded.sha256_pair,
            model         = excluded.model,
            tier          = excluded.tier,
            summary_md    = excluded.summary_md,
            input_tokens  = excluded.input_tokens,
            output_tokens = excluded.output_tokens,
            chunk_count   = excluded.chunk_count,
            generated_at  = excluded.generated_at
        """,
        [
            bill_id, from_code, to_code, sha_pair, model, result.tier,
            result.summary_md, result.input_tokens, result.output_tokens,
            result.chunk_count, datetime.now(tz=timezone.utc),
        ],
    )


# ---------------------------------------------------------------------------
# Tier dispatch
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def pick_tier(combined_tokens: int) -> tuple[str, str]:
    """Return (tier, model) for the given combined token count."""
    if combined_tokens <= HAIKU_SINGLE_BUDGET:
        return "single", HAIKU_MODEL
    if combined_tokens <= SONNET_SINGLE_BUDGET:
        return "single", SONNET_MODEL
    if combined_tokens <= SONNET_CHUNKED_BUDGET:
        return "chunked", SONNET_MODEL
    return "punt", ""


# OpenRouter approximate pricing per million tokens (USD). Update if it shifts.
_PRICING = {
    HAIKU_MODEL:  (1.00, 5.00),     # Haiku 4.5
    SONNET_MODEL: (3.00, 15.00),    # Sonnet 4.6
}


def estimate_cost_usd(combined_tokens: int,
                      *, output_tokens: int = 1500) -> tuple[str, str, float]:
    """Return (tier, model, estimated_usd) for a call covering ``combined_tokens``
    of input. ``output_tokens`` is the upper-bound on completion length we
    plan to allocate.
    """
    tier, model = pick_tier(combined_tokens)
    if tier == "punt":
        return tier, model, 0.0
    in_rate, out_rate = _PRICING.get(model, (3.00, 15.00))
    if tier == "chunked":
        # One call per chunk + one meta-summary call. Estimate # chunks.
        chunks = max(1, (combined_tokens + CHUNK_TOKEN_TARGET - 1) // CHUNK_TOKEN_TARGET)
        in_tokens = combined_tokens + chunks * 500    # prompt overhead per chunk
        out_tokens = chunks * 600 + output_tokens     # per-chunk + meta
    else:
        in_tokens = combined_tokens + 500
        out_tokens = output_tokens
    cost = (in_tokens * in_rate + out_tokens * out_rate) / 1_000_000
    return tier, model, cost


def summarize_delta(store: "Store", bill_id: str, from_code: str, to_code: str,
                    from_label: str, to_label: str,
                    from_body: str, to_body: str,
                    *, force: bool = False) -> Optional[SummaryResult]:
    """Generate (or fetch cached) AI delta summary. Returns None if API key
    is missing — caller should render an "AI summary unavailable" stub.

    Model + chunking strategy are auto-selected by combined token count.
    """
    sha_pair = sha256_pair(from_body, to_body)
    combined_tokens = estimate_tokens(from_body) + estimate_tokens(to_body)
    tier, model = pick_tier(combined_tokens)

    if not force:
        cached = get_cached(store, bill_id, from_code, to_code, sha_pair)
        if cached and cached.get("model") == model:
            return SummaryResult(
                summary_md=cached["summary_md"],
                tier=cached["tier"],
                input_tokens=cached["input_tokens"],
                output_tokens=cached["output_tokens"],
                chunk_count=cached["chunk_count"],
            )

    if tier == "punt":
        result = SummaryResult(
            summary_md=(
                f"_This bill is too large for an AI-generated change summary "
                f"({combined_tokens // 1000}k tokens combined across both versions, "
                f"exceeds even 1M-context models). "
                f"See the CRS summary above for the official neutral description._"
            ),
            tier="punt",
            input_tokens=0,
            output_tokens=0,
            chunk_count=0,
        )
        store_summary(store, bill_id, from_code, to_code, sha_pair, "n/a", result)
        return result

    client = _client()
    if client is None:
        return None

    if tier == "single":
        result = _single_call(
            client, model, bill_id, from_code, to_code,
            from_label, to_label, from_body, to_body,
        )
    else:  # chunked
        result = _chunked_call(
            client, model, bill_id, from_code, to_code,
            from_label, to_label, from_body, to_body,
        )

    store_summary(store, bill_id, from_code, to_code, sha_pair, model, result)
    return result


# ---------------------------------------------------------------------------
# Passed-bills batch walker
# ---------------------------------------------------------------------------

def find_passed_bills(store: "Store") -> list[dict]:
    """Return list of {bill_id, from_code, to_code, ..., cosponsor_count,
    latest_action_date} for bills that have advanced to enrolled or law.

    "Passed" defined as: bill_text table contains an `enr` or `pl` row, OR
    `bills.latest_action_text` indicates becoming law. Each item also carries
    a popularity proxy (cosponsor_count) and recency (latest_action_date) so
    callers can rank/sample.
    """
    rows = store.conn.execute(
        """
        WITH passed AS (
            SELECT DISTINCT bt.bill_id
            FROM bill_text bt
            WHERE bt.version_code IN ('enr', 'pl')
            UNION
            SELECT b.bill_id
            FROM bills b
            WHERE LOWER(b.latest_action_text) LIKE '%became public law%'
               OR LOWER(b.latest_action_text) LIKE '%signed by president%'
        )
        SELECT p.bill_id,
               array_agg(bt.version_code ORDER BY bt.version_code),
               array_agg(LENGTH(bt.body) ORDER BY bt.version_code),
               COALESCE(b.cosponsor_count, 0),
               b.latest_action_date,
               b.title
        FROM passed p
        JOIN bill_text bt ON bt.bill_id = p.bill_id
        LEFT JOIN bills b ON b.bill_id = p.bill_id
        GROUP BY p.bill_id, b.cosponsor_count, b.latest_action_date, b.title
        """,
    ).fetchall()
    out = []
    for bill_id, codes, sizes in rows:
        codes = list(codes)
        sizes = [int(x) for x in sizes]
        if len(codes) < 2:
            continue
        # earliest = first in STAGE_ORDER, latest = last
        from conductor.politics.bill_text import STAGE_ORDER
        order = {c: i for i, c in enumerate(STAGE_ORDER)}
        sorted_codes = sorted(codes, key=lambda c: order.get(c, 999))
        from_code = sorted_codes[0]
        to_code = sorted_codes[-1]
        from_size = sizes[codes.index(from_code)]
        to_size = sizes[codes.index(to_code)]
        combined_tokens = (from_size + to_size) // _CHARS_PER_TOKEN
        out.append({
            "bill_id": bill_id,
            "from_code": from_code,
            "to_code": to_code,
            "from_size": from_size,
            "to_size": to_size,
            "combined_tokens": combined_tokens,
        })
    return out


def estimate_batch_cost(plan: list[dict]) -> dict:
    """Sum cost estimates across a passed-bills plan. Returns
    {total_usd, by_tier, by_model, count_punt}.
    """
    total = 0.0
    by_tier: dict[str, float] = {}
    by_model: dict[str, float] = {}
    count_punt = 0
    for item in plan:
        tier, model, usd = estimate_cost_usd(item["combined_tokens"])
        item["tier"] = tier
        item["model"] = model
        item["est_usd"] = usd
        if tier == "punt":
            count_punt += 1
        total += usd
        by_tier[tier] = by_tier.get(tier, 0.0) + usd
        if model:
            by_model[model] = by_model.get(model, 0.0) + usd
    return {
        "total_usd": total,
        "by_tier": by_tier,
        "by_model": by_model,
        "count": len(plan),
        "count_punt": count_punt,
    }


# ---------------------------------------------------------------------------
# OpenRouter client
# ---------------------------------------------------------------------------

_client_cache: list = []  # singleton holder; deferred import to avoid hard dep


def _client():
    if _client_cache:
        return _client_cache[0]
    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        logger.warning("OPENROUTER_API_KEY not set; bill summaries unavailable")
        return None
    try:
        from openai import OpenAI  # type: ignore
    except ImportError:
        logger.warning("openai SDK not installed; bill summaries unavailable")
        return None
    c = OpenAI(
        api_key=api_key,
        base_url=OPENROUTER_BASE_URL,
        default_headers={
            "HTTP-Referer": os.environ.get("TALLYHQ_PUBLIC_URL", "https://tallyhq.org"),
            "X-Title": "TallyHQ",
        },
        timeout=120.0,  # 2 min — large bills can take 30-60s, give some headroom
    )
    _client_cache.append(c)
    return c


def _chat(client, model: str, system: str, user: str,
          *, max_tokens: int = 1200) -> tuple[str, int, int]:
    extra_headers = {}
    # Sonnet 4.6+ supports 1M context via beta header. Pass-through via
    # OpenRouter's extra_headers. Harmless on smaller calls.
    if "sonnet" in model.lower():
        extra_headers["anthropic-beta"] = "context-1m-2025-08-07"

    resp = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        max_tokens=max_tokens,
        temperature=0.2,
        extra_headers=extra_headers or None,
    )
    text = resp.choices[0].message.content or ""
    usage = getattr(resp, "usage", None)
    in_tok = int(getattr(usage, "prompt_tokens", 0) or 0) if usage else 0
    out_tok = int(getattr(usage, "completion_tokens", 0) or 0) if usage else 0
    return text.strip(), in_tok, out_tok


# ---------------------------------------------------------------------------
# Tier 1 — single call
# ---------------------------------------------------------------------------

_SYSTEM_DELTA = (
    "You are a nonpartisan legislative analyst writing for an audience that "
    "is not familiar with how Congress works. Explain, in plain English, "
    "what changed between two versions of a US bill — focusing strictly on "
    "policy substance.\n\n"
    "Strict rules:\n"
    "1. IGNORE formatting changes. Do not mention GPO formatting, capitalization, "
    "spelled-out vs numeric section labels, the removal of the 'Introduced by' "
    "block, the addition of a passage statement, or any cosmetic differences. "
    "If the only changes are formatting, your entire answer is a single "
    "sentence: 'No substantive changes — only standard reformatting between "
    "stages.' Stop there.\n"
    "2. IGNORE procedural changes (e.g. effective dates shifting by days, "
    "section renumbering) UNLESS they have real-world impact. If they don't, "
    "do not mention them.\n"
    "3. LEAD with the most consequential policy or scope change. What does "
    "the later version do that the earlier one didn't, or vice versa?\n"
    "4. Plain English. Avoid jargon ('appropriations', 'authorization', "
    "'reauthorization', 'continuing resolution') unless you immediately "
    "explain what it means in everyday terms.\n"
    "5. Be neutral. No partisan framing, no motives.\n"
    "6. 2-3 short paragraphs MAX. If the answer is short, the answer is short. "
    "Do not pad."
)


def _user_prompt_full(bill_id: str, from_label: str, to_label: str,
                      from_body: str, to_body: str) -> str:
    return (
        f"Bill: {bill_id}\n"
        f"Comparison: **{from_label}** → **{to_label}**\n\n"
        f"--- Earlier version ({from_label}) ---\n{from_body}\n\n"
        f"--- Later version ({to_label}) ---\n{to_body}\n\n"
        f"What changed in policy substance between these two versions? "
        f"Apply the strict rules above — skip formatting and procedural noise. "
        f"If only formatting changed, your entire answer is the one-sentence "
        f"sentinel from rule 1."
    )


def _single_call(client, model: str, bill_id: str,
                 from_code: str, to_code: str,
                 from_label: str, to_label: str,
                 from_body: str, to_body: str) -> SummaryResult:
    user = _user_prompt_full(bill_id, from_label, to_label, from_body, to_body)
    text, in_tok, out_tok = _chat(client, model, _SYSTEM_DELTA, user)
    return SummaryResult(
        summary_md=text,
        tier="single",
        input_tokens=in_tok,
        output_tokens=out_tok,
        chunk_count=1,
    )


# ---------------------------------------------------------------------------
# Tier 2 — chunked
# ---------------------------------------------------------------------------

_TITLE_RE = re.compile(r"^\s*TITLE\s+[IVXLCDM]+\b", re.IGNORECASE | re.MULTILINE)
_SEC_RE = re.compile(r"^\s*SEC(?:TION|\.)\s+\d+", re.IGNORECASE | re.MULTILINE)


def _split_by_titles(body: str) -> list[tuple[str, str]]:
    """Split a bill body into (heading, content) pairs by 'TITLE I/II/...' or
    fall back to numbered sections. Always returns at least one chunk.
    """
    if not body:
        return [("(empty)", "")]
    matches = list(_TITLE_RE.finditer(body))
    if len(matches) < 2:
        matches = list(_SEC_RE.finditer(body))
    if len(matches) < 2:
        return [("(whole bill)", body)]
    out = []
    for i, m in enumerate(matches):
        start = m.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        # Heading = first line of the chunk
        chunk = body[start:end]
        heading = chunk.split("\n", 1)[0].strip()[:120] or f"Section {i+1}"
        out.append((heading, chunk))
    return out


def _pack_chunks(parts: list[tuple[str, str]],
                 budget_tokens: int) -> list[list[tuple[str, str]]]:
    """Greedy bin-pack (heading, content) parts into groups under budget."""
    groups: list[list[tuple[str, str]]] = []
    current: list[tuple[str, str]] = []
    current_tokens = 0
    for heading, content in parts:
        n = estimate_tokens(content)
        if current and current_tokens + n > budget_tokens:
            groups.append(current)
            current = []
            current_tokens = 0
        current.append((heading, content))
        current_tokens += n
    if current:
        groups.append(current)
    return groups


def _chunked_call(client, model: str, bill_id: str,
                  from_code: str, to_code: str,
                  from_label: str, to_label: str,
                  from_body: str, to_body: str) -> SummaryResult:
    """Chunk both versions by title, pair-wise summarize matching titles,
    then meta-summarize the partial summaries into one cohesive answer.
    """
    from_parts = _split_by_titles(from_body)
    to_parts = _split_by_titles(to_body)

    # Match by heading text where possible, else by index. Headings are
    # usually stable across versions ("TITLE I—FOO"); when they aren't, we
    # fall back to positional pairing.
    from_by_head = {h.upper(): (h, c) for h, c in from_parts}
    matched: list[tuple[str, str, str]] = []  # (heading, from_chunk, to_chunk)
    used_from = set()
    for h, c in to_parts:
        key = h.upper()
        if key in from_by_head and key not in used_from:
            used_from.add(key)
            matched.append((h, from_by_head[key][1], c))
        else:
            matched.append((h, "", c))
    # Also surface from-only (deleted) titles
    for h, c in from_parts:
        if h.upper() not in used_from:
            matched.append((h, c, ""))

    # Pack into groups respecting per-call budget — combined chunk pair must
    # fit. Use 2x budget per group since we send both from and to.
    pair_groups = _pack_chunks(
        [(h, f"{f}\n{t}") for h, f, t in matched],
        CHUNK_TOKEN_TARGET,
    )

    # Generate per-group partial summaries
    partials: list[str] = []
    total_in = 0
    total_out = 0
    chunks_done = 0
    for group in pair_groups:
        chunk_text_parts = []
        for heading, _ in group:
            # Find back the matched triple by heading — small list, fine.
            for h, f, t in matched:
                if h == heading:
                    chunk_text_parts.append(
                        f"### {h}\n\n--- earlier ({from_label}) ---\n{f or '(absent)'}\n\n"
                        f"--- later ({to_label}) ---\n{t or '(absent)'}\n"
                    )
                    break
        user = (
            f"Bill: {bill_id}\n"
            f"Comparison: **{from_label}** → **{to_label}** "
            f"(this is one chunk of a multi-chunk summary)\n\n"
            + "\n\n".join(chunk_text_parts)
            + "\n\nSummarize what changed in this chunk in 1–3 short paragraphs. "
              "Focus on substantive policy/scope changes, not formatting. "
              "If nothing meaningful changed in a section, you may omit it."
        )
        text, in_tok, out_tok = _chat(client, model, _SYSTEM_DELTA, user, max_tokens=800)
        partials.append(text)
        total_in += in_tok
        total_out += out_tok
        chunks_done += 1
        logger.info("[bill_summary] %s/%s→%s chunk %d/%d done (%d/%d tok)",
                    bill_id, from_code, to_code, chunks_done, len(pair_groups),
                    in_tok, out_tok)

    # Meta-summarize: ask the model to produce one cohesive 2–4 paragraph
    # narrative from the partial summaries.
    if len(partials) == 1:
        # Single chunk — partial IS the final summary.
        summary_md = partials[0]
    else:
        meta_user = (
            f"Bill: {bill_id} — combined version delta {from_label} → {to_label}\n\n"
            f"Below are partial summaries, one per chunk of the bill. Produce a "
            f"single cohesive 3–5 paragraph summary in plain English explaining "
            f"the most important changes overall. Lead with the most consequential. "
            f"Don't enumerate every chunk — synthesize.\n\n"
            + "\n\n---\n\n".join(f"## Chunk {i+1}\n\n{p}" for i, p in enumerate(partials))
        )
        meta_text, in_tok, out_tok = _chat(
            client, model, _SYSTEM_DELTA, meta_user, max_tokens=1500,
        )
        total_in += in_tok
        total_out += out_tok
        summary_md = meta_text

    return SummaryResult(
        summary_md=summary_md,
        tier="chunked",
        input_tokens=total_in,
        output_tokens=total_out,
        chunk_count=len(pair_groups),
    )
