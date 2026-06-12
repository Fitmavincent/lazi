# Session log — 2026-06-12: Coles V2.5 crawler fix + fetch-triggered refresh

**Goals (from project owner):**
1. Make the Coles V2.5 crawl work again, aligned with the existing (frozen)
   data structure, avoiding bot detection, using a modern stealth headless
   browser. No residential proxies / third-party crawl services.
2. The Fly.io machine sleeps (intentionally), so the in-process Wednesday
   cron never fires. Add logic to the data-fetch endpoints so a stale week
   triggers a re-crawl. Specials reset Wednesday 00:00 AEST.

No deployment was performed — everything verified locally. Production was
only read (GET endpoints) for diagnosis.

---

## Diagnosis

| Check | Result |
|---|---|
| Local crawl, old stack (scrapling 0.2.99 + camoufox), headless, residential IP | **Works** — 57 tiles page 1, 249 tiles over 5 pages, zero blocks |
| Production R2 data (`GET /coles-data-v2-5`) | `synced_at: 2026-03-10` — 3 months stale |
| Production legacy data (`/coles-data`, `/coles-data-v2`) | also 2026-03-10 |
| Production Woolies | 2026-05-24 — ~3 weeks stale (machine occasionally awake on a non-Wednesday pattern) |
| `price_was` extraction | **Broken even locally** — Coles moved the was-price into a nested `<span class="price__was">`; scrapling `.text` reads only own text → every product had `price_was: 0.0` (confirmed in the March production data too) |

Conclusion: two independent failures —
(a) crawls from Fly's datacenter IP get blocked (stale Camoufox fingerprint
makes it worse; can't reproduce locally from residential IP), and the cron
that would run them rarely fires anyway because the machine sleeps;
(b) the was-price extraction regressed due to a Coles markup change.

## Changes

### Goal 1 — crawler (see ADR-001)

- `api/requirements.txt` — `scrapling[fetchers]==0.4.9` (patched-Chromium
  stealth engine, current fingerprints), camoufox/aiohttp removed,
  `curl_cffi` 0.15.0 (fixes 2 GHSA advisories), `setuptools>=78.1.1`
  (CVE-2025-47273), playwright/patchright pinned to scrapling's versions.
  Supply-chain check on all crawl deps via OSV.dev: clean.
- `api/Dockerfile` — drop camoufox fetch + blanket browser install; now
  `playwright install --with-deps firefox` + `scrapling install`.
- `api/services/special_crawler/coles_crawler_v2_5.py` — rewritten fetch
  layer:
  - one persistent `AsyncStealthySession` for warmup + all pages
    (consistent fingerprint, cookies; ~3× faster; one browser launch
    instead of ~22)
  - `locale en-AU`, `timezone Australia/Sydney`, Google referer, randomised
    human delays, per-page retry with 30/90s backoff on block detection
  - dedupe by product link; stop early when a page adds no new products
    (pagination end) or after 3 consecutive failed pages
  - **fixed was-price extraction** (`get_all_text()` over the
    `price__calculation_method` subtree)
  - scrapling 0.4 API migration (`css(...).first`, `.html_content`)
  - mirrors output to the legacy R2 key in the frozen envelope so
    `/coles-data` and `/coles-data-v2` serve the same fresh crawl
- `api/services/special_crawler/debug_coles_crawler_v2_5.py` — now reuses
  the production class with local-file storage (`python -m
  services.special_crawler.debug_coles_crawler_v2_5 [pages] [--headed]`).

### Goal 2 — fetch-triggered refresh (see ADR-002)

- `api/services/freshness.py` — Wednesday-00:00-Sydney staleness boundary;
  handles naive legacy timestamps; `/health` report helper.
- `api/services/refresh_manager.py` — single-flight + 30-min-cooldown
  background crawl trigger.
- `api/services/registry.py` — shared crawler/manager instances for both the
  request path and the scheduler (one lock for both).
- `api/main.py` — every data GET serves stored data immediately and triggers
  a background re-crawl when stale; all Coles `/sync` POSTs route to V2.5;
  `/health` now reports per-retailer freshness + refresh status; internal
  metadata stripped on every data endpoint (frozen shape preserved).
- `api/scheduler.py` — V1/V2 jobs retired; Wednesday 00:05 (Woolies) /
  00:15 (Coles) + 06:00 conditional retry, all through the shared managers;
  documented as best-effort when the machine happens to be awake.

### Tests (new, `api/tests/`, run with `pytest`)

- `test_freshness.py` — reset-boundary math (Wednesday edge cases, naive
  timestamps, the Wednesday-morning-stale scenario)
- `test_refresh_manager.py` — single-flight, cooldown, failure isolation,
  shutdown
- `test_api_endpoints.py` — frozen response shape on all data endpoints,
  stale→trigger / fresh→no-trigger, 404-still-triggers, repeated fetches
  trigger once, `/health` freshness
- `test_extractor.py` — extraction regression against a saved live-page
  snapshot (`tests/fixtures/coles_specials_snapshot.html`)
- `requirements-dev.txt`, `pytest.ini` added.

## Verification performed

- `pytest`: **30/30 pass** (no network needed).
- Full live headless crawl via debug crawler: **971 products, 20/20 pages,
  0 blocks, ~6.5 min**, frozen-shape validation PASS, 100% of spot-checked
  products with correct `price_was` after the fix (page-1 sample: 57/57).
- Local uvicorn boot: scheduler starts, `/health` reports freshness, a GET
  on missing data returns 404 *and* starts the background refresh
  (single-flight verified in logs and tests).

## Open items / risks

- **Fly IP reputation can't be tested locally.** If Coles still blocks the
  datacenter IP with the new stack, the fetch-trigger retries every 30 min
  on traffic; escalation path is V3 (proxy/Firecrawl) per the existing
  design doc.
- Machine auto-stop can kill a background crawl mid-run (~6 min crawl).
  Accepted for now; progressive per-page R2 saves are the documented next
  step if needed.
- Woolies crawler untouched (Playwright Firefox, was working); it now
  benefits from the same fetch-triggered refresh.
- Local debug artifacts (`coles_*_debug.html/json`, `repro_page.html`) are
  scratch files; `coles_specials_v2_5_debug.json` is regenerated by the
  debug crawler.

## 2026-06-13 pre-deploy dependency alignment

Clean-room verification (fresh Python 3.12 venv, install from
`api/requirements.txt` ex

```
fly deploy --config api/fly.toml   # from api/ — Dockerfile changed, image rebuilds
```
Then verify: `GET /health` (freshness block), wait for a stale fetch or
`POST /coles-data-v2-5/sync`, re-check `/health` → `is_stale: false`.
