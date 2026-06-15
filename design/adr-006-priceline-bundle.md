# ADR-006: Priceline crawler bundle

**Status:** Accepted
**Date:** 2026-06-15
**Relates to:** [adr-003](adr-003-woolies-stealth-xhr-capture.md), [adr-004](adr-004-all-discounts-discount-type.md), [adr-005](adr-005-chemist-warehouse-bundle.md)

## Context

Add Priceline as a fourth retailer with the same bundle/flow/structure as the
others (stealth crawler, GET/`sync`/`test` endpoints, registry + RefreshManager,
scheduler, `/health`, frozen shape + `discount_type`, tests).

## Research findings

- Priceline runs on **SAP Commerce (Hybris) with the OCC REST API** at
  `api.priceline.com.au`. Sale page: `/c/sale`. The OCC product search endpoint
  is `/occ/v2/priceline/products/search?query=:relevance:allCategories:sale&pageSize=36&currentPage=N`.
- Product fields: `name`, `code`, `url` (relative `/product/...`),
  `price.value` (regular/was), `discountedPrice.value` (sale/now),
  `images[].url` (relative `/medias/...`, prefer the `product` 300×300 PRIMARY),
  `brandName`. `pagination.totalResults=3871, totalPages=108` for sale.
- **Two dead ends** ruled out the Woolies/CW capture approach:
  1. Navigating the top frame to the API URL serves the SPA HTML, not JSON.
  2. Capturing the auto-fired `products/search` XHR yields an **empty body**.

## Decision — in-page `fetch` (a different mechanism, same flow)

The user explicitly allowed different implementations per retailer. Priceline's
crawler loads `/c/sale` in the stealth session, then calls the OCC API **from
inside the page** via `page.evaluate(fetch(...))`. Because the request runs
from the `www.priceline.com.au` origin (same as the SPA), CORS allows it and it
returns JSON. Key detail: a **bare GET** (no custom headers) avoids a CORS
preflight that the API rejects.

Pagination runs as a JS loop inside one `page_action` (fetch `currentPage`
0..N, accumulate, respect an in-page time budget), and the combined JSON is
stashed in a DOM node the Python side reads. Python then does extraction,
dedupe (by `code`/`url`), `discount_type` classification, the frozen envelope,
R2 storage, and the outer `MAX_CRAWL_SECONDS=600` wall-time bound — identical to
the other crawlers.

Mapping: now = `discountedPrice.value`, was = `price.value`, keep only
`was > now`, link = base + `url`, image = media base + best PRIMARY image,
`discount_type` via the shared classifier, retailer = "Priceline".

### Bundle wiring (same pattern as the others)

- `services/registry.py` — `priceline_crawler_service` + `priceline_refresh`.
- `main.py` — `GET /priceline-data` (stale-trigger), `POST /priceline-data/sync`,
  `GET /test/priceline-crawl`, `priceline` block in `/health`. Frozen shape,
  internal metadata stripped.
- `scheduler.py` — Wednesday 00:35 crawl + shared 06:00 conditional retry.
- `tests/test_priceline_extractor.py` + `priceline_search_snapshot.json` fixture.

## Consequences

- Priceline serves the same `{synced_at, count, data}` shape with
  `discount_type`; the frontend needs zero new handling.
- The in-page JS pagination differs from the other crawlers' per-page
  navigation, but the public flow and output are identical. The JS loop is
  bounded by both `max_pages` and an in-page time budget (kept under the page
  timeout) plus the outer wall-time deadline.
- price_per_unit is empty (OCC search hits carry no unit-price string).
