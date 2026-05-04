"""One-shot AI summary preview — bypasses DB so it can run while CRS pull is
holding the DuckDB writer lock.

Usage:
    set OPENROUTER_API_KEY=...
    python scripts/sample_ai_summary.py 119:hr:1968 ih enr

Reads text from data/bill_text (relative to cwd or $TALLYHQ_TEXT_ROOT) and
prints the generated summary. Costs a few cents per call.
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from conductor.secrets import load_dotenv
# Try .env in cwd first (e.g. conductor/.env), then tallyhq repo root
load_dotenv(Path.cwd() / ".env")
load_dotenv(Path(__file__).parent.parent / ".env")

from conductor.politics.bill_text import (
    DEFAULT_TEXT_ROOT, STAGE_LABELS, text_path,
)
from conductor.politics.bill_summary import (
    HAIKU_MODEL, SONNET_MODEL, _client, _chat, _SYSTEM_DELTA,
    _user_prompt_full, _split_by_titles, estimate_tokens, pick_tier,
    estimate_cost_usd,
)


def main():
    if len(sys.argv) != 4:
        print("usage: sample_ai_summary.py BILL_ID FROM_CODE TO_CODE")
        print('       sample_ai_summary.py "119:hr:1968" ih enr')
        return 2

    bill_id = sys.argv[1]
    from_code = sys.argv[2]
    to_code = sys.argv[3]

    parts = bill_id.split(":")
    if len(parts) != 3:
        print(f"bad bill_id: {bill_id}")
        return 2
    congress, bill_type, number = parts

    root = DEFAULT_TEXT_ROOT
    from_path = text_path(root, congress, bill_type, number, from_code)
    to_path = text_path(root, congress, bill_type, number, to_code)
    if not from_path.exists():
        print(f"missing: {from_path}")
        return 1
    if not to_path.exists():
        print(f"missing: {to_path}")
        return 1

    from_body = from_path.read_text(encoding="utf-8")
    to_body = to_path.read_text(encoding="utf-8")
    combined = estimate_tokens(from_body) + estimate_tokens(to_body)
    tier, model, cost = estimate_cost_usd(combined)
    print(f"bill: {bill_id}")
    print(f"versions: {from_code} ({len(from_body)} chars) → "
          f"{to_code} ({len(to_body)} chars)")
    print(f"combined: {combined} tokens — tier={tier}, model={model}, "
          f"est ${cost:.4f}")
    print()

    if not os.environ.get("OPENROUTER_API_KEY"):
        print("OPENROUTER_API_KEY not set — cannot call API.")
        return 1
    if tier == "punt":
        print("Bill too large for auto-summary.")
        return 0

    client = _client()
    if client is None:
        print("OpenAI client unavailable.")
        return 1

    from_label = STAGE_LABELS.get(from_code, from_code)
    to_label = STAGE_LABELS.get(to_code, to_code)
    user = _user_prompt_full(bill_id, from_label, to_label, from_body, to_body)

    print(f"calling {model}...")
    t0 = time.time()
    text, in_tok, out_tok = _chat(client, model, _SYSTEM_DELTA, user, max_tokens=1500)
    elapsed = time.time() - t0
    print(f"done in {elapsed:.1f}s — {in_tok} input, {out_tok} output tokens")
    print()
    print("=" * 70)
    print(text)
    print("=" * 70)
    print()
    # actual cost
    if "haiku" in model.lower():
        in_rate, out_rate = 1.0, 5.0
    else:
        in_rate, out_rate = 3.0, 15.0
    actual = (in_tok * in_rate + out_tok * out_rate) / 1_000_000
    print(f"actual cost: ~${actual:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
