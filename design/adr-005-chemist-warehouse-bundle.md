# ADR-005: Chemist Warehouse crawler bundle

**Status:** Accepted
**Date:** 2026-06-15
**Relates to:** [adr-001](adr-001-scrapling-0.4-stealth-stack.md), [adr-003](adr-003-woolies-stealth-xhr-capture.md), [adr-004](adr-004-all-discounts-discount-type.md)

## Context

Add Chemist Warehouse (CW) as a third retailer with the **same** bundle as
Coles/Woolies: a stealth crawler, GET/`sync`/`test` endpoints, registry +
`RefreshManager` wiring, scheduler jobs, `/health` freshness, the frozen data
structure + nullable `discount_type`, and tests.

## Research findings

- CW is a **Next.js app behind Cloudflare**. Category pages load products from
  **Algolia** (`42np1v2i98-dsn.algolia.net/1/indexes/*/queries`). This is the
  Woolworths situation (shadow/JS-rendered, data in an XHR), so the same
  capture approach applies — capture the Algolia response, not scrape HTML.
- The Algolia hit shape:
  - `name.en` → name
  - `calculatedPrice` → current price, **in cents**
  - `pricesCustomFields.AUD.rrp.centAmount` → RRP/was, **in cents**
  - `images[0]` → image (absolute URL)
  - `slug.en` = `"<id>-<name-slug>"` → product link `/buy/<id>/<name-slug>`
  - `nbHits=1999, nbPages=50, hitsPerPage=20` for the clearance category;
    `?page=N` pagination works (verified zero overlap between pages).
- **Source choice — clearance category** (`/shop-online/3240/clearance`). CW
  shows an RRP comparison on nearly every product (their "always cheap"
  positioning), so a general category would be almost the whole catalogue, not
  "specials". The clearance category is CW's genuine markdown set — the honest
  analogue to Coles `/on-special` and Woolies half-price. (More discount
  categories can be added later as a multi-category sweep, like Woolies.)

## Decision

`ChemistWarehouseCrawler` mirrors `WooliesCrawler` exactly:

- **Engine:** `AsyncStealthySession` (patched Chromium), `capture_xhr="algolia.net"`,
  `wait_selector="a[href*='/buy/']"`, plus **`solve_cloudflare=True`** (CW is
  Cloudflare-protected; `timeout=60000` gives the solver headroom — it's a
  no-op when no challenge is present).
- **Output:** identical frozen envelope + metadata
  (`crawler_version="cw-v1"`) and identical product shape incl. `discount_type`.
  Prices converted cents→dollars. Only `was > now` items kept; `discount_type`
  via the shared `classify_discount`.
- **Same machinery:** single persistent session, warmup, per-page retry with
  20/45s block backoff, dedupe by product link, early stop on no-new-products,
  partial-save thresholds, `MAX_CRAWL_SECONDS=600` wall-time bound,
  preserve-on-failure. `force_sync()`/`fetch_data()` signatures match the
  others.

### Bundle wiring (same pattern as Coles/Woolies)

- `services/registry.py` — `chemist_warehouse_crawler_service` +
  `chemist_warehouse_refresh` (shared by request and cron paths).
- `main.py` — `GET /chemist-warehouse-data` (stale-trigger),
  `POST /chemist-warehouse-data/sync`, `GET /test/chemist-warehouse-crawl`,
  and a `chemist_warehouse` block in `/health`. Internal metadata stripped;
  frozen shape preserved.
- `scheduler.py` — Wednesday 00:25 crawl + the shared 06:00 conditional retry,
  both routed through the RefreshManager.
- `tests/test_chemist_warehouse_extractor.py` + `cw_algolia_snapshot.json`
  fixture.

## Consequences

- CW serves the same `{synced_at, count, data}` shape with `discount_type`
  (`discount` / `half_price` / `beyond_half`), so the frontend renders it with
  zero new handling.
- Subject to the same Fly datacenter-IP / wall-time bound as the others
  (bounded partial saves). Cloudflare adds some per-page latency via the
  solver.
- price_per_unit is empty for CW (Algolia hits carry no unit-price string);
  it's a valid empty string in the frozen shape.
