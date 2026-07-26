# Putting Cloudflare in front of TallyHQ

Railway stays the origin. Cloudflare becomes the cache + bot filter in front of it.
Nothing about the app's architecture changes — DuckDB, the volume, and the nightly
`daily-update` all stay exactly where they are.

## Why

Measured 2026-07-26 from the app's own `/stats`:

| Window | Human views | Unique visitors | Bot views | % bot |
|---|---|---|---|---|
| 7d  | 7,963  | 6,239  | 28,955  | 78% |
| 30d | 21,297 | 13,701 | 104,455 | 83% |
| 90d | 57,986 | 37,344 | 314,607 | 84% |

Top crawlers over 90d: GPTBot 55.9k, ClaudeBot 53.6k, Bingbot 30.2k, Amazonbot
17.2k, Applebot 16.2k, MJ12bot 16.2k, Bytespider 14.1k.

Every one of those hit a DuckDB query, because before this change **no HTML route
set a `Cache-Control` header** — so no CDN would hold a page even if one were in
front. Meanwhile the data only moves once nightly at `DAILY_UPDATE_HOUR_UTC`.

The Free plan covers all of this. The $5 Workers Paid plan is **not** needed —
it's only relevant if we later go the static-export route.

## What the app now does

- All 200 responses: `Cache-Control: public, max-age=0, s-maxage=86400, stale-while-revalidate=604800`
  - `max-age=0` → browsers always revalidate, so a reader never sees a stale page
  - `s-maxage` → what the **edge** actually holds
- 404s: `public, max-age=0, s-maxage=3600` (absorbs crawler path-probing)
- `/stats`: `private, no-store` — token-gated, must never reach a shared cache
- Non-GET and 5xx: `no-store`
- After a successful `daily-update`, the app purges the edge (see step 9)

Tunable via `EDGE_TTL_SECONDS` / `EDGE_SWR_SECONDS`. Set `EDGE_TTL_SECONDS=0` to
disable edge caching without a redeploy of logic.

## Current DNS (before)

```
tallyhq.org.  A  69.46.46.96          # Railway edge
NS: ns1.vercel-dns.com, ns2.vercel-dns.com
```

Railway origin target: `tallyhq-production.up.railway.app`

Cloudflare's proxy requires **its own nameservers** on the Free/Pro plans —
CNAME-only ("partial") setup is Business-tier. So the nameservers move off Vercel.
Vercel stays the *registrar*; only DNS hosting moves.

---

## Steps

### 1. Add the zone

Cloudflare dashboard → **Add a site** → `tallyhq.org` → **Free**.
Cloudflare scans existing DNS. Confirm it imported the apex `A` record.

### 2. Get the DNS records right — grey cloud first

In Cloudflare **DNS → Records**, make sure you have the apex record pointing at
Railway. Either form works; the CNAME is preferable (Cloudflare flattens at apex,
and it survives Railway changing edge IPs):

```
CNAME   @   tallyhq-production.up.railway.app   Proxy: DNS only (grey)
```

Cross-check against Railway → service → Settings → Networking → Custom Domain,
which shows the exact records it wants. **If Railway lists an `_acme-challenge`
CNAME, copy it over too** — it's what lets Railway issue the Let's Encrypt cert.

Leave everything **grey-clouded (DNS only)** for now. Proxy comes on in step 5,
after the cert exists.

### 3. Move nameservers at Vercel

Vercel dashboard → **Domains** → `tallyhq.org` → **Nameservers** → Custom, and
enter the two Cloudflare nameservers from step 1.

Propagation is usually minutes, up to a few hours. Cloudflare emails you when the
zone goes active. Verify:

```bash
nslookup -type=NS tallyhq.org 8.8.8.8      # expect *.ns.cloudflare.com
curl -sI https://tallyhq.org | grep -i server   # still railway-hikari (grey cloud)
```

Site should keep working normally throughout — grey-cloud is plain DNS.

### 4. SSL/TLS mode — use **Full**, not Full (Strict)

SSL/TLS → Overview → **Full**.

> Railway is explicit that with the Cloudflare proxy on, **Full (Strict) does not
> work as intended.** For proxied domains Railway may not be able to issue a cert
> for the custom hostname; Cloudflare↔Railway traffic is still TLS-encrypted using
> Railway's default `*.up.railway.app` certificate, which Strict rejects.

Also set Edge Certificates → **Always Use HTTPS: On**.

### 5. Turn the proxy on

Flip the apex record to **Proxied (orange)**. Verify Cloudflare is now in path:

```bash
curl -sI https://tallyhq.org | grep -iE "cf-ray|cf-cache-status|server"
```

`cf-ray` present ⇒ proxied.

**If Railway's cert gets stuck on "Validating Challenges":** grey-cloud the record,
wait for Railway to issue the cert, then orange-cloud again. This can recur at
renewal (~30 days before expiry) — see Gotchas.

### 6. Cache Rule — Cloudflare does *not* cache HTML by default

This is the step that actually does the work. Without it, `s-maxage` is ignored
for `text/html` and nothing gets cached.

Caching → **Cache Rules** → Create rule:

- **Name:** `Cache HTML`
- **Expression:**
  ```
  (http.host eq "tallyhq.org" and not starts_with(http.request.uri.path, "/stats"))
  ```
- **Cache eligibility:** Eligible for cache
- **Edge TTL:** Use cache-control header if present, use default otherwise
- **Browser TTL:** Respect origin TTL

Excluding `/stats` in the rule is belt-and-braces — the app already sends
`private, no-store` — but a token-bearing URL in a shared cache is bad enough to
guard twice.

### 7. Browser Cache TTL → "Respect Existing Headers"

Caching → **Configuration** → Browser Cache TTL → **Respect Existing Headers**.

Do not skip this. A fixed zone-wide value silently rewrites `Cache-Control` on
every response leaving the edge, including our `max-age=0`, and pins readers to a
stale copy for hours with no way to scope it per-path. This exact failure cost a
debugging session on spiritvalers.com — see the org KB note
`cloudflare-zone-browser-cache-ttl-clobbers-workers`.

### 8. Block *training* crawlers — not retrieval crawlers

**Do not use the blanket "Block AI Scrapers and Crawlers" toggle.** It is too
coarse and will block the AI bots that actually send readers. Use
Security → **AI Crawl Control**, which lists each detected crawler with its own
Block toggle, and decide per bot.

The distinction that matters: OpenAI runs **GPTBot** for *training*,
**OAI-SearchBot** for ChatGPT *search*, and **ChatGPT-User** for *a human who
just clicked a link*. Different pipelines, different bots. You can starve
training while staying fully visible in the answer engines that cite you.

**Block — training only, zero referral traffic (~151k/90d, 48% of bot load):**

| Bot | 90d views |
|---|---|
| GPTBot | 55,923 |
| ClaudeBot | 53,604 |
| Amazonbot | 17,238 |
| Bytespider | 14,126 |
| Meta-ExternalAgent | 10,522 |

Also `CCBot`, `Google-Extended` and `Applebot-Extended` if they appear — the
`-Extended` variants are *training-only* opt-outs and blocking them does **not**
affect Google Search or Siri ranking.

**Block — SEO backlink resellers, no value to us (~25k/90d, 8%):**
MJ12bot 16,186 · SemrushBot 5,662 · AhrefsBot 2,277 · serpstatbot 987.
`robots.txt` already asks these to leave (commit `d015f46`); they ignore it.
Cloudflare enforces rather than asks.

**Allow — these send real traffic (~17k/90d, only 5% of bot load):**

| Bot | 90d views | Why |
|---|---|---|
| Googlebot | 12,062 | organic search — top referrer |
| Bingbot | 30,222 | Bing, and it feeds ChatGPT search + Copilot |
| OAI-SearchBot | 10,493 | ChatGPT search — cites with links |
| ChatGPT-User | 3,447 | **a person clicked through from ChatGPT** |
| PerplexityBot | 2,727 | Perplexity cites sources prominently |
| Applebot | 16,204 | Siri / Spotlight |
| DuckDuckBot | 123 | search |

Keeping all of these costs ~5% of bot load. Blocking them costs discovery.

> **Check the defaults — don't assume "off".** Since 2025-07-01 Cloudflare blocks
> GPTBot, ClaudeBot and **PerplexityBot** by default on *new* zones, and a mid-2026
> change extended default edge-blocking across free and paid plans. `tallyhq.org`
> is a new zone, so PerplexityBot may already be blocked and need
> *un*-blocking. Read the current state in AI Crawl Control before changing anything.

Separately, Security → **Bots** → **Bot Fight Mode** handles generic
non-AI junk. It allowlists verified search engines, so it's safe alongside the above.

### 8b. Caching alone will not carry this — the page count works against it

Tempting conclusion: "the data changes once a day, so a 24h cache fixes
everything." That holds for a small site. It does **not** hold here, and the
reason is the site's size.

A cache only helps on the *second* request for a URL inside the TTL window.
TallyHQ has ~19,000 cacheable URLs (15,902 bills + 536 legislators + 536 SVGs +
~1,500 roll calls + indexes) against ~4,140 requests/day. A crawler doing a broad
sweep fetches each deep URL **once** — that request is a MISS, populates the
cache, and nothing asks for it again before the nightly purge wipes it. GPTBot's
55,923 requests over 90 days is ~621/day; at that rate a full pass over the bill
pages takes ~26 days, so essentially none of its traffic hits a warm cache.

Where caching *does* win: human traffic, which is heavily concentrated
(`/legislator/L000598` 2,668 views, `/` 2,650, `/bills` 833 — the top few paths
carry a large share), plus the indexes and any URL several different bots happen
to hit within the same day.

Rough expectation: caching removes on the order of **40–70%** of origin requests.
Blocking training + SEO crawlers removes **~47% of all traffic outright**, and
those never touch origin *or* cache. The two are complementary, not substitutes:

| Lever | What it removes |
|---|---|
| Caching (step 6) | repeat hits — humans, indexes, popular pages |
| Blocking (step 8) | broad deep-crawl sweeps that caching structurally cannot dedupe |

Ship caching first because it is zero-risk and helps every visitor. But do not
expect it to solve the bill on its own — for a site with far more pages than
daily visitors, blocking the training sweeps is doing at least as much work.

Related tuning: purging *everything* nightly is simple and always correct, but it
discards ~19,000 warm entries to reflect a change in maybe a few dozen. If cache
hit rate turns out to be the binding constraint, the upgrade is a longer
`s-maxage` plus **selective** purge (by URL prefix for changed bills/roll calls)
instead of `purge_everything`.

For TallyHQ the block/allow asymmetry is unusually clean: the content is *current*
congressional data, so being frozen into a model checkpoint is worth close to
nothing, while being retrievable live is exactly how someone finds "how did my rep
vote on HR 21" today.

### 9. Purge-on-update credentials

Create a scoped token: Cloudflare → My Profile → **API Tokens** → Create Token →
Custom token.

- **Permissions:** Zone → Cache Purge → Purge
- **Zone Resources:** Include → Specific zone → `tallyhq.org`

Grab the **Zone ID** from the zone's Overview page (right sidebar), then:

```bash
railway variables --service tallyhq \
  --set CLOUDFLARE_ZONE_ID=<zone-id> \
  --set CLOUDFLARE_PURGE_TOKEN=<token>
```

With both set, a successful `daily-update` flushes the edge, so the 24h `s-maxage`
never actually serves stale data. Unset ⇒ purge is a logged no-op, and pages age
out naturally instead.

### 10. Verify

```bash
# Header present, edge in path
curl -sI https://tallyhq.org/ | grep -iE "cache-control|cf-cache-status|cf-ray"

# Second hit should be HIT
curl -sI https://tallyhq.org/bill/119/hr/21 | grep -i cf-cache-status
curl -sI https://tallyhq.org/bill/119/hr/21 | grep -i cf-cache-status

# Stats must never be cached
curl -sI "https://tallyhq.org/stats?token=REDACTED" | grep -iE "cache-control|cf-cache-status"
```

Expect `cf-cache-status: MISS` then `HIT`, and `private, no-store` + `BYPASS` on
`/stats`.

Then watch Railway CPU/memory over a few days and right-size down.

---

## Gotchas

- **Your `/stats` numbers will crater — that's the mechanism working, not traffic
  loss.** `_record_page_view` only records requests that reach the origin. Once the
  edge absorbs them, origin analytics measures cache misses, not readers. Add
  Cloudflare **Web Analytics** (free, Analytics → Web Analytics) for real numbers,
  and treat `/stats` as an origin-load gauge from here on.

- **Railway cert renewal behind the proxy.** Issuance and renewal both run
  Let's Encrypt challenges that Cloudflare intercepts. If a renewal fails, temporarily
  grey-cloud the record until it completes. Worth a calendar reminder ~11 months out,
  or check Railway's domain page occasionally.

- **`s-maxage=86400` plus purge-on-write is deliberate.** Long hold, explicit
  invalidation. If you ever run `daily-update` manually via `railway ssh` or the CLI,
  the in-process purge does *not* fire — purge by hand from the Cloudflare dashboard
  (Caching → Configuration → Purge Everything).

- **Bot Fight Mode issues JS challenges.** The `/api/*` JSON routes are consumed by
  the site's own pages (same-origin, fine), but any external script hitting them will
  start failing. Nothing known does today.

- **`analytics.duckdb` is at 42 MB and grows unbounded** — a row per request via the
  `_record_page_view` middleware. Caching slows the growth but doesn't stop it. Needs a
  retention policy independently of this work.

---

## Phase C — move the registrar to Cloudflare (optional)

Not required for caching. Do it **after** the zone is active on Cloudflare
nameservers — Cloudflare Registrar refuses a transfer otherwise, so the ordering
is forced regardless.

### Eligibility — checked 2026-07-26 via RDAP

| Requirement | Status |
|---|---|
| Registrar | Name.com, Inc. (IANA 625) — Vercel resells through them |
| Registered ≥ 60 days ago | ✓ 2026-05-03, **84 days** |
| Not transferred in last 60 days | ✓ |
| Not transfer-locked | ✓ status is `active`, no `clientTransferProhibited` |
| DNSSEC disabled | ✓ `delegationSigned: false` — no 24h wait |
| TLD supported | ✓ `.org` |
| Zone active on Cloudflare DNS | ✗ **blocked on Phase A** |
| Payment method on Cloudflare | verify before starting |

Expiry is 2027-05-03; the transfer adds a year on top.

> Note: RDAP showed `last changed 2026-07-26T17:46Z`, minutes before this check —
> so something touched the domain that day. If that was an unlock done by hand,
> good; if not, re-check the status codes before starting.

### Steps

1. **Finish Phase A** and wait for Cloudflare to report the zone **Active**
   (minutes, occasionally up to 24h).

2. **Get the auth code from Vercel.** Dashboard → **Domains** → `tallyhq.org` →
   **⋯** menu → *Transfer out*. A modal shows the EPP/authorization code.
   Team owners only. There is no `vercel domains transfer-out` CLI command —
   `transfer-in` exists, the reverse does not, so this step is dashboard-only.

3. **Start the transfer at Cloudflare.** Dashboard → **Domain Registration** →
   **Transfer Domains** → select `tallyhq.org` → paste the auth code → confirm
   contact details and payment.

4. **Approve.** Name.com emails a transfer confirmation — approving it releases
   the domain immediately instead of waiting out the registry's 5-day auto-approve.

5. **Expect up to 10 days** end to end; ~30 minutes of actual work.

### Cost

Cloudflare Registrar sells at cost with no markup — `.org` lands around
$10–12/yr versus the ~$20+/yr typical Name.com/Vercel renewal. Modest saving,
but the real reason to do it is having DNS and registration in one place.

### Safety

DNS does **not** break during a registrar transfer: nameservers are already
Cloudflare's from Phase A, and the transfer moves only the registration record.
The site keeps serving throughout.

Before starting, confirm `tallyhq.org` isn't attached to a live Vercel *project*
(Vercel dashboard → the domain's project bindings). It shouldn't be — Railway
serves the site and Vercel only sells the domain — but a stale binding is worth
clearing first.

## Rollback

Any single step reverts cleanly:

- Cache behaving badly → Caching → Configuration → **Purge Everything**, or set
  `EDGE_TTL_SECONDS=0` on Railway
- Proxy causing trouble → flip the record back to **grey cloud**; Cloudflare drops
  out of the path entirely, app unaffected
- Full revert → point nameservers back to `ns1/ns2.vercel-dns.com` at Vercel

## Sources

- [Railway — Working with Domains](https://docs.railway.com/networking/domains/working-with-domains)
- [Railway — Troubleshooting SSL](https://docs.railway.com/networking/troubleshooting/ssl)
- [Railway Central Station — Cloudflare SSL "Full (Strict)" does not work](https://station.railway.com/questions/cloudflare-ssl-full-strict-does-not-ab35244a)
