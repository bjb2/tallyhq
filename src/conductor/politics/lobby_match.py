"""Bill-lobbied match scoring.

LDA filings often reference stale bill numbers because lobbyists copy
descriptions year-over-year without updating. The regex extracts the
number, pairs with `filing_year → congress`, and produces a bill_id that
may point at a completely unrelated bill that happens to share the
number in the current Congress.

Real example (2026-05-03): Best Buy 2025 Q1 description said
"S.140/HR.895: Combating Organized Retail Crime Act, ...". 119:hr:895
in the 119th Congress is "Ensuring Justice for Victims of Partial-Birth
Abortion Act" — wrong bill, lobbyist using stale boilerplate.

Three signals, weighted-summed, threshold-filtered:

  1. title-keyword match — does the mention text share content words
     with the bill's title?
  2. issue-code vs policy-area — soft check; LDA `general_issue_code`
     loosely correlates with bill `policy_area`.
  3. bill-name in mention — token-overlap similarity between mention
     text and bill title (fuzzy).

Score in [0, 1]. Three bands:
  >= 0.6   confident match (show)
  0.3-0.6  possibly related (show with asterisk)
  <  0.3   likely false positive (hide on bill page)
"""
from __future__ import annotations

import re
from dataclasses import dataclass


# ---------------------------------------------------------------------------
# Issue-code → expected policy-areas
# ---------------------------------------------------------------------------
# LDA general_issue_code is a 3-letter code from a fixed 76-code list.
# Bill policy_area is one of ~32 LoC policy areas. Map is intentionally
# multi-valued — many issue codes legitimately span several policy areas.
# Source for issue codes: https://lda.senate.gov/api/v1/constants/filing/lobbyingactivityissues/
ISSUE_TO_POLICY: dict[str, set[str]] = {
    "AGR": {"Agriculture and Food"},
    "ANI": {"Animals", "Agriculture and Food"},
    "APP": {"Economics and Public Finance", "Government Operations and Politics"},
    "ART": {"Arts, Culture, Religion"},
    "AUT": {"Transportation and Public Works", "Commerce"},
    "AVI": {"Transportation and Public Works"},
    "BAN": {"Finance and Financial Sector"},
    "BEV": {"Agriculture and Food", "Health"},
    "BNK": {"Finance and Financial Sector"},
    "BUD": {"Economics and Public Finance"},
    "CAW": {"Animals"},
    "CDT": {"Finance and Financial Sector", "Commerce"},
    "CHM": {"Environmental Protection", "Health"},
    "CIV": {"Civil Rights and Liberties, Minority Issues"},
    "CIVIL": {"Civil Rights and Liberties, Minority Issues"},
    "CON": {"Government Operations and Politics"},
    "CPI": {"Commerce", "Civil Rights and Liberties, Minority Issues"},
    "CPT": {"Commerce", "Science, Technology, Communications"},
    "CSP": {"Sports and Recreation"},
    "DEF": {"Armed Forces and National Security"},
    "DIS": {"Health", "Social Welfare"},
    "DOC": {"Commerce"},
    "ECN": {"Economics and Public Finance"},
    "EDU": {"Education"},
    "ENG": {"Energy"},
    "ENV": {"Environmental Protection", "Public Lands and Natural Resources"},
    "FAM": {"Families", "Social Welfare"},
    "FIN": {"Finance and Financial Sector"},
    "FIR": {"Emergency Management", "Public Lands and Natural Resources"},
    "FOO": {"Agriculture and Food", "Health"},
    "FOR": {"International Affairs", "Foreign Trade and International Finance"},
    "FUE": {"Energy"},
    "GAM": {"Sports and Recreation", "Native Americans"},
    "GOV": {"Government Operations and Politics"},
    "HCR": {"Health"},
    "HOM": {"Emergency Management", "Armed Forces and National Security"},
    "HOU": {"Housing and Community Development"},
    "IMM": {"Immigration"},
    "IND": {"Native Americans"},
    "INS": {"Finance and Financial Sector", "Health"},
    "INT": {"Intellectual Property"},
    "LAW": {"Crime and Law Enforcement"},
    "LBR": {"Labor and Employment"},
    "MAN": {"Commerce"},
    "MAR": {"Transportation and Public Works"},
    "MED": {"Science, Technology, Communications", "Commerce"},
    "MIA": {"Armed Forces and National Security"},
    "MMM": {"Public Lands and Natural Resources", "Energy"},
    "MON": {"Finance and Financial Sector"},
    "NAT": {"Public Lands and Natural Resources", "Native Americans"},
    "PHA": {"Health"},
    "POS": {"Government Operations and Politics"},
    "REL": {"Arts, Culture, Religion"},
    "RES": {"Public Lands and Natural Resources"},
    "RET": {"Labor and Employment", "Finance and Financial Sector"},
    "ROD": {"Transportation and Public Works"},
    "RRR": {"Transportation and Public Works"},
    "SCI": {"Science, Technology, Communications"},
    "SMB": {"Commerce"},
    "SPO": {"Sports and Recreation"},
    "TAR": {"Foreign Trade and International Finance", "Commerce"},
    "TAX": {"Taxation"},
    "TEC": {"Science, Technology, Communications"},
    "TOB": {"Health", "Agriculture and Food"},
    "TOR": {"Crime and Law Enforcement"},
    "TOU": {"Sports and Recreation", "Commerce"},
    "TRA": {"Transportation and Public Works"},
    "TRD": {"Foreign Trade and International Finance"},
    "TRU": {"Transportation and Public Works"},
    "UNM": {"Labor and Employment"},
    "URB": {"Housing and Community Development"},
    "UTI": {"Energy", "Science, Technology, Communications"},
    "VET": {"Armed Forces and National Security"},
    "WAS": {"Environmental Protection"},
    "WEL": {"Social Welfare"},
}


# ---------------------------------------------------------------------------
# Stopwords + tokenization
# ---------------------------------------------------------------------------
# Generic words that show up in nearly every bill title — useless for
# overlap signal. Bill-specific cruft like "Act", "Reform" included.
STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "the", "and", "or", "but", "of", "for", "to", "in",
    "on", "at", "by", "with", "from", "as", "is", "are", "was",
    "were", "be", "been", "being", "have", "has", "had", "do", "does",
    "did", "this", "that", "these", "those", "it", "its", "they",
    "them", "their", "we", "us", "our", "you", "your", "he", "she",
    "his", "her", "i", "me", "my", "act", "bill", "amendment",
    "amendments", "amend", "reform", "law", "laws", "section", "title",
    "public", "general", "any", "all", "other", "such", "shall", "may",
    "must", "no", "not", "only", "than", "then", "if", "when", "while",
    "issues", "related", "relating", "regarding", "concerning", "about",
    "various", "matters", "provisions", "policy", "policies", "house",
    "senate", "rep", "sen", "hr", "s", "hjres", "sjres", "hconres",
    "sconres", "hres", "sres", "h", "j", "res", "con", "co", "etc",
    "see", "also", "et", "al", "monitoring", "monitor", "tracking",
    "discussion", "discussions", "implementation", "implement",
    "impact", "impacts", "impacted", "affecting", "affect", "include",
    "including", "included", "support", "oppose", "opposed", "supports",
})

_BILL_REF_RE = re.compile(
    r"(?i)\b(?:H\.?\s?R\.?|S\.?|H\.?\s?J\.?\s?Res\.?|S\.?\s?J\.?\s?Res\.?|"
    r"H\.?\s?Con\.?\s?Res\.?|S\.?\s?Con\.?\s?Res\.?|H\.?\s?Res\.?|"
    r"S\.?\s?Res\.?)\s*\d{1,5}\b"
)
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z'-]+")


def _tokens(text: str) -> set[str]:
    if not text:
        return set()
    cleaned = _BILL_REF_RE.sub(" ", text)
    return {
        t.lower() for t in _TOKEN_RE.findall(cleaned)
        if len(t) > 2 and t.lower() not in STOPWORDS
    }


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

@dataclass
class MatchScore:
    score: float                # weighted, in [0, 1]
    title_overlap: float        # signal 1 — Jaccard
    issue_policy: float         # signal 2 — 0/0.5/1
    name_in_mention: float      # signal 3 — fraction of title tokens present in mention
    has_mention: bool           # whether mention text was available
    band: str                   # "confident" | "possible" | "false_positive"


# Weights chosen so signal #1 (the strongest) dominates when present,
# but the score still leans on issue/policy when mention text is missing.
W_TITLE = 0.50
W_NAME = 0.30
W_ISSUE = 0.20

# When mention is missing, fall back to issue-code only with rescaled weight.
W_ISSUE_FALLBACK = 1.0


def score_match(
    *,
    bill_title: str,
    bill_policy_area: str,
    issue_codes: list[str],
    mention_text: str = "",
) -> MatchScore:
    """Score a bill_lobbied event against the bill it points to.

    Returns a MatchScore with sub-signals exposed for diagnostics.
    """
    title_tokens = _tokens(bill_title)
    mention_tokens = _tokens(mention_text)

    # Signal 1: Jaccard of title vs mention
    if title_tokens and mention_tokens:
        inter = title_tokens & mention_tokens
        union = title_tokens | mention_tokens
        title_overlap = len(inter) / len(union) if union else 0.0
    else:
        title_overlap = 0.0

    # Signal 2: issue-code → policy-area expectation
    expected: set[str] = set()
    for code in issue_codes:
        expected |= ISSUE_TO_POLICY.get(code.upper(), set())
    if not bill_policy_area or not expected:
        # No info either way — neutral.
        issue_policy = 0.5
    elif bill_policy_area in expected:
        issue_policy = 1.0
    else:
        issue_policy = 0.0

    # Signal 3: fraction of title content tokens present in mention
    if title_tokens and mention_tokens:
        hit = sum(1 for t in title_tokens if t in mention_tokens)
        name_in_mention = hit / len(title_tokens)
    else:
        name_in_mention = 0.0

    has_mention = bool(mention_tokens)
    if has_mention:
        score = (
            W_TITLE * title_overlap
            + W_NAME * name_in_mention
            + W_ISSUE * issue_policy
        )
    else:
        # Degraded mode — only signal 2 is informative. Anchor at 0.45 so a
        # neutral (no info) match falls into "possible" not "confident".
        score = 0.15 + 0.45 * issue_policy

    score = max(0.0, min(1.0, score))
    if score >= 0.6:
        band = "confident"
    elif score >= 0.3:
        band = "possible"
    else:
        band = "false_positive"

    return MatchScore(
        score=score,
        title_overlap=title_overlap,
        issue_policy=issue_policy,
        name_in_mention=name_in_mention,
        has_mention=has_mention,
        band=band,
    )


# Display thresholds — used by lobby_views to filter what shows on each surface.
THRESHOLD_BILL_PAGE_SHOW = 0.6     # bill page main list
THRESHOLD_BILL_PAGE_POSSIBLE = 0.3  # bill page "possibly related" section
THRESHOLD_CLIENT_PAGE = 0.0         # client profile shows everything (it's their data)
