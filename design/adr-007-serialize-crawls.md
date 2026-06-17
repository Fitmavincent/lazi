# ADR-007: Serialize crawls globally + harden Coles against navigation timeouts

**Status:** Accepted
**Date:** 2026-06-17
**Relates to:** [adr-002](adr-002-fetch-triggered-refresh.md), [adr-004](adr-004-all-discounts-discount-type.md)

## Context

Production logs (Fly, Wed 2026-06-17) showed the Coles crawl failing:

```
Starting V2.5 crawl pipeline (up to 50 pages, single session)
ERROR: Failed after 1 attempts: Page.goto: Timeout 60000ms exceeded.
  - navigating to "https://www.coles.com.au/on-special?page=2", waiting until "load"
... Page 2 empty (non-block). Retrying in 10s.
... Loading data from Cloudflare R2          # <- a concurrent fetch_data, mid-crawl
```

Two things stand out:
1. **The `goto` blocks on the page `load` event.** scrapling navigates with
   `page.goto(url)` (Playwright default `wait_until="load"`) and applies our
   `timeout` as the navigation timeout — there's no config to relax it. Coles'
   `/on-special` pages keep loading ad/tracking/Incapsula resources, so `load`
   doesn't fire within 60s and `goto` times out **even though the product DOM is
   already present**.
2. **Concurrency.** A "60s timeout" logged ~2s after pipeline start, and
   `Loading data from Cloudflare R2` (a `fetch_data` read) interleaved with the
   crawl — i.e. several operations running at once. This regressed when the
   frontend's weekly-resync change shipped: on Wednesday every retailer is
   stale, so the frontend polls all four data endpoints, triggering up to four
   browser crawls **concurrently** on the 4 GB Fly machine. The resulting
   CPU/memory contention slows page rendering enough to push Coles' `load` wait
   past the timeout.

When pages time out, each costs up to 3×60s of retries; the crawl burns its
wall-time budget, ends below the 50-item save threshold, and saves nothing —
so last week's specials stay stale.

## Decision

### 1. Serialize crawls globally (primary fix)

`RefreshManager` gains a class-level single-flight slot (`_global_active`): a
trigger is rejected while **any** retailer's crawl is running, not just its own.
Claimed synchronously at trigger time (no await in between, so two triggers in
one tick can't both win) and released in the crawl's `finally`. Self-healing —
a stale reference whose task has finished doesn't block.

Effect: only one headless-browser crawl runs at a time across the app, so each
crawl gets the machine to itself and navigations complete in time. The
frontend's 90s polls and the Wednesday cron naturally pick up the other
retailers once the current crawl finishes (subject to the 30-min per-retailer
cooldown).

### 2. Coles failure economics (defense in depth)

- `MAX_PAGE_RETRIES` 2 → 1: a page that times out once is almost certainly being
  throttled; two more full-timeout retries only waste the budget.
- `MIN_PRODUCTS_TO_SAVE` 50 → 30: a partially-throttled run still persists fresh
  data instead of discarding it and leaving the week stale.

`timeout` is left at 60s — with crawls serialized, legitimate pages render well
within it; lowering it risked killing slow-but-valid loads.

## Alternatives considered

- **Navigate with `wait_until="domcontentloaded"`** (the genuine root-cause fix
  for the `load` hang). scrapling hardcodes the `goto` wait and exposes no
  override; doing it ourselves means driving navigation inside `page_action`
  against a separate light landing page (the Priceline pattern). Higher risk /
  bigger rewrite; deferred. If serialization doesn't fully resolve it, this is
  the next step — scrapling builds its `Response` from the live `page.content()`
  after `page_action`, so it's viable.
- **Lower `timeout`** — rejected as primary (kills valid slow loads); the retry
  cut achieves most of the time-saving more safely.

## Consequences

- Weekly refresh is now sequential: on Wednesday the four retailers refresh
  one after another (driven by frontend polls / cron) rather than all at once.
  Slower wall-clock to refresh everything, but each crawl is far more likely to
  succeed. Correctness over speed.
- `tests/test_refresh_manager.py` covers the global single-flight (and resets
  the class-level slot between tests).
