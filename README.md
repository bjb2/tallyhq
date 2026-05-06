# TallyHQ

**Live at [tallyhq.org](https://tallyhq.org)**

> Congress, on the record. A GitHub-style activity tracker for the US Congress.

Every roll-call vote, every bill introduced, every party-line break, every floor speech — pulled from official sources, attributed to each member, rendered as a daily record of who is doing what on Capitol Hill.

![TallyHQ landing — Congress, on the record.](docs/landing.png)

## What it shows

- **Per-legislator activity grid** (GitHub-contribution-style 7×N heatmap) over the past year
- **Roll-call detail pages** with party-by-party tallies and break-vote highlighting
- **Bill detail pages** with sponsor, cosponsors, action timeline (stage-coded), text-version downloads, and an aggregate Cook PVI "cosponsor lean" stat
- **Bills index** searchable by title, filterable by chamber + type + policy area
- **Browse** members with committee/subcommittee filtering, live aggregate stats, and a PVI pill per card
- **Landing dashboard** — congressional pulse heatmap, top-active / most-absent / biggest-party-breakers rankings, recent break-votes timeline
- **Committee leadership rings** — gold ring around chairs, silver around ranking members, on every sponsor/cosponsor/legislator portrait
- **Cook PVI lean** per district + state, surfaced on legislator profiles, browse cards, and bill sponsor cards

## Architecture

Single Python service (FastAPI + Jinja templates) with a single embedded DuckDB file for storage. Append-only event store with `(source, source_id, payload_hash)` dedupe. Adapters fan structured data out to per-legislator events that all surfaces (grid, profile, browse aggregates) compute over.

### Data sources

| Source | What | Free | Key required |
|---|---|---|---|
| `clerk.house.gov` XML | House roll-call votes | ✓ | no |
| `senate.gov` XML | Senate roll-call votes | ✓ | no |
| `govinfo.gov BILLSTATUS` | Bill metadata + sponsor + cosponsors + actions | ✓ | no |
| `govinfo.gov CREC` | Floor speeches (Congressional Record metadata) | ✓ | no |
| `api.congress.gov` | Amendments + bill enrichment | ✓ | yes |
| `lda.senate.gov` | Lobbying disclosures | ✓ | optional (raises rate limit) |
| `api.open.fec.gov` | Campaign finance totals | ✓ | yes |
| `unitedstates/congress-legislators` (YAML) | Member roster + committee assignments + ID crosswalks | ✓ | no |
| `unitedstates/images` (gh-pages) | Member portraits | ✓ | no |
| `en.wikipedia.org` (Cook PVI article) | Cook Partisan Voting Index per district + state | ✓ | no |

## Quick start (local)

```bash
pip install -e .

# 1. Seed entity tables
conductor politics sync-legislators
conductor politics sync-committees
conductor politics sync-pvi          # Cook PVI per district + state

# 2. Pull events
conductor politics bulk-bills --congress 119 --bill-types all   # ~30 min
conductor pull congress_rollcalls
conductor pull senate_rollcalls
conductor pull congress_amendments
conductor pull govinfo_crec        # floor speeches
conductor politics sync-funding --cycles 2026,2024

# 3. Run web
conductor politics web   # http://127.0.0.1:8770
```

## Environment

Copy `.env.example` to `.env` and fill in:

- `CONGRESS_GOV_API_KEY` — required for amendments + member-photo fallback (free at https://api.congress.gov/sign-up/)
- `OPENFEC_API_KEY` — required for campaign-finance totals (free at https://api.open.fec.gov/developers)
- `LDA_API_KEY` — optional, raises lobbying-disclosure rate limit (free at https://lda.senate.gov/api/register/)

## Deployment

See `DEPLOY.md` for Railway setup (web service + cron service sharing one persistent volume).

## License

[PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0/). Free for personal use, research, hobby projects, and any charitable / educational / public-research / government organization. Commercial use requires a separate license — open an issue or reach out.

Note: prior commits were released under MIT and remain available under those terms; relicensing governs the source from this LICENSE change forward.
