# Deploying TallyHQ to Railway

> **CDN:** Cloudflare sits in front of Railway as cache + bot filter.
> See [CLOUDFLARE.md](CLOUDFLARE.md) — origin architecture below is unchanged.

Two services share one Dockerfile + one persistent volume.

```
Vercel domain (CNAME)
       │
       ▼
Railway (project: tallyhq)
   ├── service: web        ←  startCommand: politics web --host 0.0.0.0 --port $PORT
   └── service: cron       ←  startCommand: politics daily-update
              └── shared volume: /data  (DuckDB file lives here)
```

## 1. Volume

Create a Railway volume named `data` mounted at `/data`. Both services attach to it.
DuckDB process-lock means **only one writer at a time** — the cron service runs
~3 minutes nightly while the web is mostly idle, so this is fine in practice
(and the web doesn't write during normal serving anyway, just photo-cache
upserts).

## 2. Web service

- Build: `Dockerfile` (root)
- Start: `python -m conductor.cli --db /data/conductor.duckdb politics web --host 0.0.0.0 --port $PORT`
- Healthcheck: `GET /` → 200
- Mount: `data` volume → `/data`
- Env:
  - `CONGRESS_GOV_API_KEY` (required for amendments + member depiction fallback)
  - `LDA_API_KEY` (optional — speeds up LDA backfill)

## 3. Cron service (daily updates)

Same repo, same Dockerfile. Different start command.

- Start: `python -m conductor.cli --db /data/conductor.duckdb politics daily-update`
- Mount: same `data` volume → `/data`
- Cron: `0 7 * * *` (07:00 UTC = 02:00 ET — chamber off-hours)
- Env: same as web
- Restart policy: `NEVER` (let cron handle re-runs)

## 4. Domain (Vercel-purchased)

Vercel sells you the domain; Railway hosts the service. Two paths:

**Path A — point domain at Railway (recommended)**
1. Vercel: domain → DNS → add `CNAME` for `@` (or `www`) pointing to your Railway public URL (`xyz.up.railway.app`).
2. Railway: web service → Settings → Custom Domain → enter the bare domain. Railway auto-issues a Let's Encrypt cert.

**Path B — Vercel as edge cache, Railway as origin**
1. Vercel project: serverless rewrite rule that proxies `/*` to Railway.
2. Useful only if you want Vercel's edge HTTP cache; adds a hop. Skip unless you've measured a need.

## 5. First-time data load (one-shot)

Local laptop → push to Railway volume by running the bulk on Railway directly:

```bash
railway run --service web python -m conductor.cli --db /data/conductor.duckdb politics sync-legislators
railway run --service web python -m conductor.cli --db /data/conductor.duckdb politics sync-committees
railway run --service web python -m conductor.cli --db /data/conductor.duckdb politics bulk-bills --congress 119 --bill-types all
railway run --service web python -m conductor.cli --db /data/conductor.duckdb pull congress_rollcalls
railway run --service web python -m conductor.cli --db /data/conductor.duckdb pull senate_rollcalls
```

Or seed locally and `scp` the DuckDB file into the volume — faster but requires shell access.

## 6. After deploy

- Web hits `/`, photos resolve via `/photo/{bioguide}` route, cached in `photo_cache` DB table.
- First request after a deploy: web warms photo cache (`load_persisted_into_memory` is instant from DB; missing entries probed in background).
- Cron service runs nightly, idempotent — re-runs are safe via cursor + payload-hash dedupe.

## 7. Cost ballpark

- Web service: ~50–200 MB RAM, low CPU. Hobby plan covers it.
- Cron: ephemeral, runs ~3 min/day. Counts against Hobby execution minutes.
- Volume: 5 GB included on Hobby; 119th alone is ~500 MB → comfortable headroom.
- Total: ~$5/mo for moderate traffic.

## 8. Pitfalls

- **DB lock**: never run `politics web` and `politics daily-update` simultaneously
  on the same volume. Cron timing avoids this naturally; if you trigger a
  manual update via `railway run` while the web is up, the web will hang on
  a write attempt until the run completes.
- **Photo cache cold start after restart**: in-memory dict is empty at boot
  but `photo_cache` DB table is populated. `load_persisted_into_memory()` runs
  on startup → resolved in <100 ms. New members not yet cached resolve
  on-demand on first request.
- **CONGRESS_GOV_API_KEY rotation**: just set the new value in Railway env;
  no redeploy needed (env reload on next service restart).
- **Volume backups**: Railway volumes don't snapshot by default. Run a periodic
  `cp /data/conductor.duckdb /data/conductor.$(date +%F).bak` step in the cron
  service if you want point-in-time recovery.
