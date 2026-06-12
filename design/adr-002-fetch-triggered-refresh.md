# ADR-002: Fetch-triggered background refresh instead of relying on in-process cron

**Status:** Accepted
**Date:** 2026-06-12
**Relates to:** Stale weekly specials data; Fly.io machine auto-sleep

## Context

The Fly.io app runs with `auto_stop_machines = true` and
`min_machines_running = 0` (intentional — keeps idle cost at zero). The
APScheduler jobs that crawl Coles/Woolies every Wednesday 00:00 AEST live
*in-process*, so they only fire if the machine happens to be awake at that
moment. In practice it almost never is: production data observed on
2026-06-12 was synced 2026-03-10 (Coles) and 2026-05-24 (Woolies).

Australian supermarket specials reset **Wednesday 00:00 local time**.

Constraints:
- Machine sleep stays (owner decision; no always-on cost).
- No external schedulers required to be set up (GitHub Actions cron remains a
  documented option but the chosen design must be self-contained).
- The frontend just fetches JSON; no frontend changes.
- The data endpoints' response shape is **frozen** (see
  `coles-crawler-v3-design.md` CONSTRAINT section).

## Decision

**Check freshness on the data-read path.** Every `GET` on a data endpoint
(`/coles-data`, `/coles-data-v2`, `/coles-data-v2-5`, `/woolies-data`):

1. Loads the stored JSON from R2 and returns it immediately, unchanged in
   shape (internal metadata fields stripped). Stale data is still served —
   slightly old specials beat an error or a 60s blocking request.
2. Compares `synced_at` against the most recent **Wednesday 00:00
   Australia/Sydney** boundary (`services/freshness.py`). Naive timestamps
   from older crawlers are treated as UTC.
3. If stale, starts the crawl as an **asyncio background task** on the
   machine that this very request just woke up (`services/refresh_manager.py`).
   The next fetch after the crawl completes (~6 min) gets fresh data.

Guard rails in `RefreshManager`:
- **Single-flight:** one crawl per retailer at a time; concurrent stale
  fetches don't stack crawls.
- **Cooldown (30 min):** a blocked/failing crawler isn't re-hammered on every
  fetch; retries happen at most twice an hour while data remains stale, until
  a crawl succeeds and writes a fresh `synced_at`.
- Crawl failures never overwrite good R2 data (the crawler only saves on
  success/partial thresholds).

**One crawl feeds all Coles endpoints.** The V2.5 crawler now mirrors its
output to the legacy R2 key (`coles_specials.json`) in the frozen envelope
(`synced_at`/`count`/`data` only), alongside its own
`coles_specials_v2_5.json` (with crawl metadata). All Coles `/sync` POST
endpoints route to the V2.5 crawler. V1/V2 crawler classes survive only as
R2 readers, so whichever endpoint the frontend uses gets fresh data.

**Scheduler kept as a bonus, routed through the same managers.** The
Wednesday cron jobs now call `RefreshManager.trigger_if_needed`, sharing the
same single-flight lock as the fetch path. A Wednesday 06:00 conditional
retry re-crawls only if data is still stale. If the machine is asleep, these
simply don't fire and the fetch path takes over.

Freshness diagnostics (staleness, age, refresh-in-progress) are exposed
**only on `GET /health`** — never on the data endpoints.

### Why not the alternatives

- **GitHub Actions cron hitting `/sync`:** works and remains documented in
  the V3 design doc, but adds an external moving part and a long-held HTTP
  connection (crawl runs inside the request); the fetch-trigger requires
  nothing outside this repo and self-heals whenever anyone uses the app.
- **`min_machines_running = 1` on Wednesdays:** ongoing cost, manual toggling.
- **Fly Machines scheduled starts:** Fly's `schedule` only supports coarse
  intervals (hourly/daily/etc.), would run the whole app container and still
  needs in-process logic to decide whether to crawl.

## Consequences

- Data freshness now depends on someone fetching after Wednesday midnight —
  acceptable: if nobody looks at the data, freshness doesn't matter; the
  first viewer Wednesday morning triggers the crawl and sees fresh data on
  their next visit/refresh (~6 min later).
- A background crawl can be killed if Fly stops the machine before it
  finishes (~6 min crawl vs. Fly's idle-stop after a few minutes without
  traffic). Mitigations: the crawl is much faster now (single browser
  session, early pagination stop); user browsing keeps traffic flowing while
  they wait; the cooldown lets the next fetch retry; `kill_timeout = 180` in
  fly.toml gives in-flight work a grace window. If this proves flaky in
  practice, the next step is progressive per-page saves to R2 with resume.
- Worst-case duplicate work across *machines* (if Fly ever runs >1) is
  acceptable: crawls are idempotent writes of the same weekly data.
