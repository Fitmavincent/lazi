# ADR-003: Align Woolies crawler with Coles (scrapling 0.4 stealth + XHR capture)

**Status:** Accepted
**Date:** 2026-06-13
**Relates to:** [adr-001](adr-001-scrapling-0.4-stealth-stack.md), [adr-002](adr-002-fetch-triggered-refresh.md)

## Context

After the Coles V2.5 rewrite, Woolies was the only retailer still on the old
stack: plain Playwright **Firefox** with no stealth, intercepting the
`apis/ui/browse/category` XHR via `page.route`. Post-deploy verification
(2026-06-13) showed it **fails from Fly's datacenter IP** — the fetch-triggered
refresh fired, ran, and produced nothing, so the preserve-on-failure logic
kept 3-week-old data. The crawler code itself was fine (78 products locally);
the failure was the non-stealth browser being blocked from the datacenter IP,
exactly like Coles was.

Goal: bring Woolies onto the **same** stealth foundation as Coles and emit the
**same** frozen data structure, so both retailers behave identically.

## Investigation findings

- The Woolies half-price page renders products into **shadow-DOM web
  components** (`<wc-product-tile>`, empty light DOM) — so HTML scraping (the
  Coles approach) cannot read product fields. The page's product data is not
  embedded in the HTML either (`DisplayName`/`IsHalfPrice`/`CupString` absent).
- The real data source is the `apis/ui/browse/category` XHR (clean JSON:
  `Bundles[].Products[]`, ~43 half-price items/page, `TotalRecordCount` ~1716
  across all specials).
- Scrapling 0.4 exposes `capture_xhr` (regex on XHR/fetch URLs) →
  `response.captured_xhr` (list of `Response` objects; body via `.json()`).

### Two scrapling gotchas found and worked around

1. **`capture_xhr` is a session-construction argument, not a per-`fetch()`
   one.** Passing it to `session.fetch()` is silently ignored (it's absent
   from the per-request override list). It must be set on
   `AsyncStealthySession(...)`. This cost the most debugging time — capture
   returned 0 until set on the constructor.
2. **The captured `Response.html_content` wraps the body**, so `json.loads()`
   on it fails. Use the Response's `.json()` method (or `.body` bytes).
3. **`network_idle=True` is harmful on Woolies.** The site streams
   ad/analytics traffic that rarely goes idle, so `network_idle` made each
   page wait near the full 60s timeout (a 30-page crawl ran 20+ min on Fly —
   long enough to risk the machine sleeping mid-crawl). Dropped it:
   `wait_selector="wc-product-tile"` already guarantees the category XHR has
   fired and been captured. With that removed, 3 pages take ~30s locally.
   `max_pages` set to 20 for parity with Coles (~600-700 products).

## Decision

Rewrite `WooliesCrawler` to mirror `ColesV25Crawler` exactly:

- **Engine:** `AsyncStealthySession` (patched Chromium) — same as Coles. Firefox
  is no longer required by Woolies.
- **Data acquisition:** stealth session with `capture_xhr="apis/ui/browse/category"`;
  navigate the half-price page per `?pageNumber=N`, read the captured category
  JSON via `.json()`, extract `IsHalfPrice` products.
- **Output:** identical frozen envelope + internal metadata
  (`synced_at, crawl_status, pages_attempted, pages_succeeded, pages_blocked,
  crawler_version="woolies-v2", count, data`) and identical product shape
  (`name, price, price_per_unit, price_was, product_link, image, discount,
  retailer`). `discount` uses the **same** semantics as Coles
  (`"Save $X.XX"` else `"Half Price"`), replacing the old hard-coded
  `"50% off"`.
- **Shared behaviour:** single persistent session, light homepage warmup,
  per-page retry with 30/90s block backoff, dedupe by product link, early stop
  on no-new-products, partial-save thresholds (save ≥50, success ≥150),
  preserve-on-failure, same R2 key (`/home/crawlers/woolies_specials.json`).
- **Interface unchanged:** `force_sync()` / `fetch_data()` keep the same
  signatures, so `registry.py`, `main.py`, the `RefreshManager`, and the
  scheduler all work without changes. The `/test/woolies-crawl` endpoint was
  updated to the new `crawl_pipeline()` shape (parallel to
  `/test/coles-crawl-v2-5`).

### Why XHR capture rather than HTML scraping (unlike Coles)

Coles renders product tiles in light DOM, so HTML scraping works there. Woolies
uses shadow-DOM components, making HTML scraping impractical — but it exposes a
clean JSON API the page already calls. Capturing that JSON through the *same
stealth browser* keeps the anti-bot posture identical to Coles while giving
more reliable structured data. "Same as Coles" is satisfied at the level that
matters: same stealth engine, same refresh strategy, same output contract.

## Consequences

- Woolies now defeats the same datacenter-IP block Coles does (verified
  locally; live verification on deploy). Subject to the same partial-blocking
  caveat as Coles — `partial` status saves what it gets.
- Old `transform_product_data()` / `handle_request()` / `process_response()` /
  `crawl_woolies_pipeline()` removed; `fake_useragent` and direct
  `playwright.async_api` use dropped from this crawler.
- Firefox is still installed in the image for the legacy V1 Coles crawler
  import; it could be dropped in a future cleanup once V1 is fully retired.
- Regression coverage: `tests/test_woolies_extractor.py` against a saved
  category-API JSON snapshot.
