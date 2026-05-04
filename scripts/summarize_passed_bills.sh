#!/usr/bin/env bash
# Summarize all passed bills using Claude Code CLI w/ Haiku.
# Fresh agent per bill (no shared context). Idempotent: skips bills that
# already have a summary.md. Saves output to {bill_dir}/summary.md.
#
# Usage:
#   bash summarize_passed_bills.sh /c/Users/bryan/enclave/conductor/data/bill_text/119

set -uo pipefail

ROOT="${1:-/c/Users/bryan/enclave/conductor/data/bill_text/119}"
if [ ! -d "$ROOT" ]; then
  echo "ROOT not found: $ROOT" >&2
  exit 1
fi

# Earliest-version search order (legislative process)
PRIORITY=(ih is rh rs eh es pcs pch)

read -r -d '' SYSTEM_PROMPT <<'EOF'
You are a nonpartisan legislative analyst writing for an audience that is not familiar with how Congress works. Explain, in plain English, what changed between two versions of a US bill — focusing strictly on policy substance.

Strict rules:
1. IGNORE formatting changes. Do not mention GPO formatting, capitalization, spelled-out vs numeric section labels, the removal of the 'Introduced by' block, the addition of a passage statement, or any cosmetic differences. If the only changes are formatting, your entire answer is a single sentence: "No substantive changes — only standard reformatting between stages." Stop there.
2. IGNORE procedural changes (e.g. effective dates shifting by days, section renumbering) UNLESS they have real-world impact. If they don't, do not mention them.
3. LEAD with the most consequential policy or scope change. What does the later version do that the earlier one didn't, or vice versa?
4. Plain English. Avoid jargon ('appropriations', 'authorization', 'reauthorization', 'continuing resolution') unless you immediately explain what it means in everyday terms.
5. Be neutral. No partisan framing, no motives.
6. 2-3 short paragraphs MAX. If the answer is short, the answer is short. Do not pad.

Return ONLY the final summary markdown — no preamble, no meta-commentary about your process.
EOF

processed=0
skipped=0
errors=0
total=$(find "$ROOT" -name "enr.txt" 2>/dev/null | wc -l)
i=0
echo "found $total enrolled bills under $ROOT"

while IFS= read -r enr; do
  i=$((i+1))
  bill_dir=$(dirname "$enr")
  number=$(basename "$bill_dir")
  bill_type=$(basename "$(dirname "$bill_dir")")
  congress=$(basename "$(dirname "$(dirname "$bill_dir")")")
  bill_id="${congress}:${bill_type}:${number}"

  if [ -f "$bill_dir/summary.md" ]; then
    skipped=$((skipped+1))
    continue
  fi

  # Find earliest version
  earliest=""
  for v in "${PRIORITY[@]}"; do
    if [ -f "$bill_dir/$v.txt" ]; then
      earliest=$v
      break
    fi
  done
  if [ -z "$earliest" ]; then
    echo "[$i/$total] $bill_id: no earlier version found, skipping"
    errors=$((errors+1))
    continue
  fi

  earlier_size=$(stat -c%s "$bill_dir/$earliest.txt" 2>/dev/null || stat -f%z "$bill_dir/$earliest.txt")
  enr_size=$(stat -c%s "$enr" 2>/dev/null || stat -f%z "$enr")
  combined=$((earlier_size + enr_size))
  # ~4 chars/token. Reserve ~10K tokens for prompt scaffolding + output.
  # Haiku 4.5 = 200K ctx → ~190K usable → 760K chars combined input.
  # Sonnet 4.6 (1M beta) = 1M ctx → ~950K usable → 3,800,000 chars combined.
  HAIKU_CHAR_BUDGET=760000
  SONNET_CHAR_BUDGET=3800000

  if [ "$combined" -le "$HAIKU_CHAR_BUDGET" ]; then
    model="haiku"
    tier="haiku"
  elif [ "$combined" -le "$SONNET_CHAR_BUDGET" ]; then
    model="sonnet"
    tier="sonnet"
  else
    echo "[$i/$total] $bill_id: too large ($combined chars, exceeds Sonnet 1M) — punt stub"
    {
      printf -- "---\nbill_id: %s\nfrom_code: %s\nto_code: enr\ngenerated_at: %s\nmodel: none\ntier: punt\nchars: %s\n---\n\n" \
        "$bill_id" "$earliest" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$combined"
      printf "_Bill exceeds 1M-token context window (%s combined characters). See CRS summary for official neutral description._\n" \
        "$combined"
    } > "$bill_dir/summary.md"
    errors=$((errors+1))
    continue
  fi

  echo "[$i/$total] $bill_id: $earliest -> enr ($combined chars, $tier)..."
  earlier_body=$(cat "$bill_dir/$earliest.txt")
  enr_body=$(cat "$enr")

  prompt=$(printf '%s\n\nBill: %s\nComparison: %s -> Enrolled\n\n--- Earlier version (%s) ---\n%s\n\n--- Later version (Enrolled) ---\n%s\n\nWhat changed in policy substance?' \
    "$SYSTEM_PROMPT" "$bill_id" "$earliest" "$earliest" "$earlier_body" "$enr_body")

  # Call claude CLI in print mode w/ chosen model
  if output=$(printf '%s' "$prompt" | claude -p --model "$model" 2>&1); then
    {
      printf -- "---\nbill_id: %s\nfrom_code: %s\nto_code: enr\ngenerated_at: %s\nmodel: claude-%s-via-cli\ntier: %s\nchars: %s\n---\n\n" \
        "$bill_id" "$earliest" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$model" "$tier" "$combined"
      printf '%s\n' "$output"
    } > "$bill_dir/summary.md"
    processed=$((processed+1))
    echo "  done ($(wc -c < "$bill_dir/summary.md") bytes)"
  else
    echo "  ERROR: $output"
    errors=$((errors+1))
  fi
done < <(find "$ROOT" -name "enr.txt" 2>/dev/null | sort)

echo ""
echo "DONE — processed=$processed skipped=$skipped errors=$errors total=$total"
