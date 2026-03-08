# Coles Crawler Design Document

**Status:** Active
**Context:** Multi-session reference document for improving Coles half-price specials crawler
**Last updated:** 2026-03-07

---

## CONSTRAINT: API Response Structure is Frozen

> **Before writing a single line of implementation code, read this section.**

The existing API response shape consumed by the frontend **must not change**. No fields may be removed, renamed, or have their type changed. No new fields may be added to the product data endpoints (`/coles-data`, `/coles-data-v2`, `/woolies-data`).

### Frozen product envelope (top-level)

```json
{
  "synced_at": "<ISO 8601 string>",
  "count": 312,
  "data": [ ... ]
}
```

### Frozen product item shape

```json
{
  "name": "string",
  "price": 0.0,
  "price_per_unit": "string",
  "price_was": 0.0,
  "product_link": "string",
  "image": "string",
  "discount": "string",
  "retailer": "string"
}
```

### Rules

1. The R2 stored JSON **may** contain extra internal metadata fields (`crawl_status`, `pages_attempted`, etc.) — that is fine for crawler diagnostics.
2. Those internal fields **must be stripped** before the API returns the response to callers, so the frontend never sees them.
3. Freshness / staleness information **must only be exposed via `GET /health`** — never injected into the product data responses.
4. The `fetch_data()` method signature and return type stays the same; freshness checking is a responsibility of the health endpoint, not the data endpoints.

**Any implementation that changes what the frontend receives must be rejected regardless of how useful the extra data might be.**

---

## 1. Problem Statement

The current Coles crawlers fail intermittently due to aggressive anti-bot protection on `coles.com.au`. Woolworths crawling is relatively stable; Coles is the primary pain point.

### 1.1 Current Crawler Summary

| Crawler | File | Approach | Storage | Failure Mode |
|---|---|---|---|---|
| V1 (ColesCrawler) | `coles_crawler.py` | Playwright (Firefox) — intercepts `**/api/product*` XHR | R2 | Single page only, no pagination, fragile header setup |
| V2 (ColesV2Crawler) | `coles_crawler_v2.py` | Scrapling `StealthyFetcher` — HTML scraping | R2 | Blocked by Incapsula ("Pardon Our Interruption") |
| Debug V2 | `debug_coles_crawler_v2.py` | Same as V2 with local file output, non-headless | Local | Same as V2 |
| Woolies | `woolies_crawler.py` | Playwright (Firefox) — intercepts `**/apis/ui/browse/category*` XHR | R2 | Mostly stable |

### 1.2 Root Causes of Failure

1. **Incapsula / Imperva bot detection** — Coles serves a "Pardon Our Interruption" interstitial when automated browsers are detected. V2 detects this string and returns empty with no retry or fallback.

2. **New browser context per page** — V2 calls `StealthyFetcher.async_fetch()` for every page number in a loop. Each call likely spawns a fresh browser process, so Coles sees 20 rapid independent browser sessions — a clear bot signal.

3. **Fixed, uniform delays** — `await asyncio.sleep(3)` between every page is perfectly regular. Real user browsing has variable timing.

4. **No session warmup** — V2 navigates directly to the specials URL without any prior browsing. Real users arrive via the homepage, search, or a referrer.

5. **Fragile CSS selectors** — The entire extraction depends on `data-testid` attributes that Coles can change any sprint without notice. No fallback if a selector misses.

6. **No retry / partial save** — A single empty result moves silently to the next page. If all pages fail, nothing is saved and there is no alert.

7. **IP reputation** — Fly.io `syd` machines run from known datacenter CIDR ranges, which Coles' bot protection scrutinises more heavily.

8. **No staleness detection on read** — The API `/coles-data-v2` endpoint serves whatever is in R2 with no indication that the data might be from last week.

---

## 2. Immediate Plan: V2.5 (No External Services)

V2.5 improves stability through better browser behaviour, retry logic, partial saves, and stale-data awareness. It stays within the existing Scrapling-based approach and introduces no new paid services.

### 2.1 Single Persistent Browser Session

**Current behaviour:** Each call to `crawl_page(page_num)` triggers a new `StealthyFetcher.async_fetch()` — a new browser process — for every page.

**V2.5 change:** Scrapling's `StealthyFetcher` supports a `persistent_context` / session mode (or use the lower-level `PlayWright` context it wraps). Crawl all pages inside **one browser session** that navigates from page to page. This:
- Presents a single consistent fingerprint across all pages.
- Accumulates cookies and local storage like a real user session.
- Reduces total browser process startup overhead.

If Scrapling does not expose persistent-session APIs directly, use its underlying `nodriver` (or Playwright) context manually and replicate the stealth patches.

**Pseudo-flow:**
```
open one StealthyFetcher session
  → navigate to homepage (warmup)
  → wait random 2–5 s
  → navigate to specials page 1
  → extract products
  → for each next page:
      → scroll to bottom of page (simulate reading)
      → wait random 4–9 s
      → navigate to next page URL (or click next button)
      → extract products
  → close session
```

### 2.2 Session Warmup

Before going to the specials URL, spend 5–10 seconds on the Coles homepage to establish cookies and appear as an organic visitor:

```python
WARMUP_URLS = [
    "https://www.coles.com.au",          # homepage
    "https://www.coles.com.au/specials",  # generic specials
]
WARMUP_WAIT = (3, 6)  # random seconds range
```

Navigate to these URLs first, wait a random duration, then proceed to the half-price specials page.

### 2.3 Human-Like Timing

Replace all fixed `asyncio.sleep(3)` with randomised delays drawn from a range that covers realistic reading time:

```python
import random

async def human_delay(min_s=4.0, max_s=9.0):
    await asyncio.sleep(random.uniform(min_s, max_s))
```

Between pages use `human_delay(4, 9)`. After a block detection, use a longer wait (`human_delay(30, 60)`) before retry.

### 2.4 Per-Page Retry on Block Detection

When "Pardon Our Interruption" or `interstitial-inprogress` is detected, do not move to the next page. Retry the same page:

```python
MAX_PAGE_RETRIES = 2
BLOCK_BACKOFF = [30, 90]  # seconds — longer waits after a block

async def crawl_page_with_retry(session, page_num) -> list[dict]:
    for attempt in range(MAX_PAGE_RETRIES + 1):
        products, blocked = await crawl_single_page(session, page_num)
        if products:
            return products
        if blocked:
            if attempt < MAX_PAGE_RETRIES:
                wait = BLOCK_BACKOFF[attempt]
                logger.warning(f"Page {page_num} blocked. Waiting {wait}s before retry {attempt+1}.")
                await asyncio.sleep(wait)
        else:
            # Non-block failure (timeout, parse error): shorter retry
            if attempt < MAX_PAGE_RETRIES:
                await asyncio.sleep(10)
    return []
```

### 2.5 Partial Save on Success Threshold

If crawling completes with fewer products than expected but above a minimum threshold, save what we have rather than discarding everything:

```python
MIN_PRODUCTS_TO_SAVE = 50    # below this = likely a full block, don't save
MIN_PRODUCTS_SUCCESS = 200   # below this = partial success, save but mark as partial
```

Logic:
```
collected >= MIN_PRODUCTS_SUCCESS → crawl_status = "success"
MIN_PRODUCTS_TO_SAVE <= collected < MIN_PRODUCTS_SUCCESS → crawl_status = "partial", save anyway
collected < MIN_PRODUCTS_TO_SAVE → crawl_status = "failed", keep old R2 data
```

### 2.6 CSS Selector Fallback Chain

Rather than a single selector per field, try a prioritised list:

```python
NAME_SELECTORS = [
    'a.product__link.product__image[aria-label]',      # current
    '[data-testid="product-tile"] h2',                 # fallback 1
    '[data-testid="product-tile"] .product__title',    # fallback 2
]

PRICE_SELECTORS = [
    '[data-testid="product-pricing"][aria-label]',     # current
    '.price__value',                                   # fallback 1
    '[class*="price"][class*="current"]',              # fallback 2
]

CONTAINER_SELECTORS = [
    'div[data-testid="specials-product-tiles"]',       # current
    '[data-testid="product-grid"]',                    # fallback 1
    'main ul[class*="product"]',                       # fallback 2
]
```

The extractor tries each selector in order and uses the first that returns a result. If all fail on a 200 response with no block page, log a `SELECTOR_FAILURE` event with the page URL so we know Coles updated their markup.

### 2.7 Realistic StealthyFetcher Settings

Review the V2 options and adjust:

| Option | Current V2 | V2.5 | Reason |
|---|---|---|---|
| `headless` | `True` | `True` (production) | Needed for server; StealthyFetcher patches headless signals |
| `humanize` | `True` | `True` | Keep |
| `geoip` | `True` | `True` | Appear as AU user |
| `google_search` | `False` | `True` | Referrer looks more organic when coming from Google |
| `block_webrtc` | `False` | `False` | Keep (blocking looks suspicious) |
| `disable_ads` | `False` | `False` | Keep |
| `timeout` | `45000` | `60000` | Give more time, Coles JS is heavy |
| `wait` | `3000` | `5000` | Wait longer for dynamic content to render |
| `locale` | not set | `en-AU` | Match expected Australian locale |
| `timezone` | not set | `Australia/Sydney` | Match expected timezone |

### 2.8 Improved Block Detection

The current check only looks for two strings. Expand to a more robust detection function:

```python
BLOCK_SIGNALS = [
    "Pardon Our Interruption",
    "interstitial-inprogress",
    "/_Incapsula_Resource",
    "visid_incap",
    "challenge-platform",
    "cf-challenge",         # Cloudflare challenge
    "Enable JavaScript",    # Blank render
]

def is_blocked(html: str) -> bool:
    return any(signal in html for signal in BLOCK_SIGNALS)

def is_empty_render(html: str) -> bool:
    """Page loaded but no product container found and page is suspiciously short"""
    return len(html) < 5000 and 'product' not in html.lower()
```

### 2.9 Crawl Metadata in Saved JSON

Add metadata fields to the R2 file to support diagnostics and staleness detection:

```json
{
  "synced_at": "2026-03-04T00:31:45Z",
  "crawl_status": "success",
  "pages_attempted": 20,
  "pages_succeeded": 18,
  "pages_blocked": 2,
  "crawler_version": "v2.5",
  "count": 312,
  "data": [...]
}
```

`crawl_status` values: `"success"` | `"partial"` | `"failed"`

---

## 3. Staleness Detection on API Read

### 3.1 Background

Coles specials reset every **Wednesday**. The cron crawler runs Wednesday at 00:00 AEST. If a caller queries the API on Wednesday at 10:00 AM and `synced_at` in R2 is from the previous Wednesday or earlier, the data is stale — the new week's specials may not be reflected.

Currently `fetch_data()` returns the raw R2 JSON with no freshness check.

### 3.2 Freshness Logic

Specials week boundaries are anchored to **Wednesday 00:00 AEST**. Data is stale if it was synced before the most recent Wednesday midnight:

```python
import pytz
from datetime import datetime, timedelta

SYDNEY_TZ = pytz.timezone('Australia/Sydney')
SPECIALS_DAY = 2  # Wednesday (Monday=0)

def get_last_specials_reset() -> datetime:
    """Returns the datetime of the most recent Wednesday 00:00 AEST."""
    now = datetime.now(SYDNEY_TZ)
    days_since_wednesday = (now.weekday() - SPECIALS_DAY) % 7
    last_wednesday = now.replace(hour=0, minute=0, second=0, microsecond=0) \
                     - timedelta(days=days_since_wednesday)
    return last_wednesday

def check_freshness(data: dict) -> dict:
    """Annotate data dict with freshness fields. Returns the same dict + fields."""
    synced_at_str = data.get('synced_at', '')
    now = datetime.now(SYDNEY_TZ)

    if not synced_at_str:
        return {
            **data,
            'is_stale': True,
            'data_age_hours': None,
            'stale_reason': 'No sync timestamp found in stored data'
        }

    synced_at = datetime.fromisoformat(synced_at_str).astimezone(SYDNEY_TZ)
    age_hours = round((now - synced_at).total_seconds() / 3600, 1)
    last_reset = get_last_specials_reset()
    is_stale = synced_at < last_reset

    stale_reason = None
    if is_stale:
        stale_reason = (
            f"Data synced {age_hours}h ago "
            f"(before this week's specials reset on "
            f"{last_reset.strftime('%A %d %b %H:%M %Z')})"
        )

    return {
        **data,
        'is_stale': is_stale,
        'data_age_hours': age_hours,
        'stale_reason': stale_reason,
    }
```

### 3.3 Where Freshness is Exposed

**The product data endpoints (`/coles-data`, `/coles-data-v2`, `/woolies-data`) must return exactly the frozen structure defined in the CONSTRAINT section above.** The `check_freshness()` function must never be called inside these endpoints.

`fetch_data()` in the crawler classes returns the R2 dict. Before returning from the endpoint, strip any internal metadata fields that were added by the crawler:

```python
INTERNAL_FIELDS = {"crawl_status", "pages_attempted", "pages_succeeded", "pages_blocked", "crawler_version"}

@app.get("/coles-data-v2")
async def read_coles_data_v2():
    data = await coles_v2_crawler_service.fetch_data()
    if not data:
        raise HTTPException(status_code=404, detail="No data available")
    # Strip internal crawler metadata before returning — frontend shape is frozen
    return {k: v for k, v in data.items() if k not in INTERNAL_FIELDS}
```

Freshness information is exposed **only** via `GET /health` (see Section 3.4 below).

### 3.4 Health Endpoint Enhancement

`GET /health` is the **only** place freshness data is surfaced. It is not consumed by the frontend product UI — it exists for monitoring and debugging. Extend it to include per-retailer freshness:

```python
@app.get("/health")
async def read_health():
    coles_data = await coles_v2_crawler_service.fetch_data()
    woolies_data = await woolies_crawler_service.fetch_data()
    return {
        "status": "ok",
        "data_freshness": {
            "coles": check_freshness(coles_data) if coles_data else {"is_stale": True, "data_age_hours": None},
            "woolies": check_freshness(woolies_data) if woolies_data else {"is_stale": True, "data_age_hours": None},
        }
    }
```

Example `/health` response (the only place `is_stale` / `data_age_hours` appear):
```json
{
  "status": "ok",
  "data_freshness": {
    "coles": {
      "synced_at": "2026-03-04T00:31:45Z",
      "crawl_status": "success",
      "is_stale": false,
      "data_age_hours": 9.5,
      "stale_reason": null
    },
    "woolies": {
      "synced_at": "2026-03-04T00:12:03Z",
      "is_stale": false,
      "data_age_hours": 9.3,
      "stale_reason": null
    }
  }
}
```

The product data endpoints return exactly what they return today — no extra fields.

---

## 4. Scheduler Improvements

### 4.1 Current Schedule

| Job | Day | Time (AEST) | Note |
|---|---|---|---|
| `fetch_coles_data` (V1) | Wednesday | 00:00 | Effectively broken, single page |
| `fetch_coles_data_v2` | Wednesday | 00:30 | Active, intermittently fails |
| `fetch_woolies_data` | Wednesday | 00:00 | Active, mostly stable |

### 4.2 Proposed V2.5 Schedule

| Job | Day | Time (AEST) | Note |
|---|---|---|---|
| `fetch_woolies_data` | Wednesday | 00:00 | Unchanged |
| `fetch_coles_data_v2_5` | Wednesday | 00:15 | V2.5 crawler (staggered 15 min) |
| `fetch_coles_retry` | Wednesday | 06:00 | Conditional retry — see below |

Retire V1 `fetch_coles_data` job (it's been superseded by V2 and contributes nothing).

### 4.3 Conditional Retry Job

The 06:00 job checks if the current R2 data is stale (i.e., the 00:15 run failed or produced only partial data) before re-crawling:

```python
async def conditional_retry_coles():
    crawler = ColesV2Crawler()  # or V2.5 class
    data = crawler.load_from_file()

    if data is None:
        logger.info("Retry: no data in R2 at all, running crawl")
        await crawler.force_sync()
        return

    freshness = check_freshness(data)
    if freshness['is_stale'] or data.get('crawl_status') == 'partial':
        logger.info(f"Retry: data is stale or partial (status={data.get('crawl_status')}), re-crawling")
        await crawler.force_sync()
    else:
        logger.info("Retry: data is fresh and complete, skipping")
```

### 4.4 Fly.io Machine Sleep Issue

**Critical constraint:** `auto_stop_machines = true` and `min_machines_running = 0` in `fly.toml` means the Fly machine can be shut down between requests. APScheduler lives in-process — if the machine is asleep at 00:15 Wednesday, the cron job never fires.

**Recommended fix (choose one):**

**Option A — Keep `min_machines_running = 1` during crawl window (simplest):**
The machine is never stopped. Increases Fly.io cost slightly but ensures scheduler fires. Given `shared-cpu-2x / 4GB`, always-on cost is ~$30-50/month for the Sydney machine.

**Option B — External trigger via GitHub Actions cron (zero idle cost):**
Add a `.github/workflows/crawl-trigger.yml`:
```yaml
on:
  schedule:
    - cron: '15 14 * * 2'   # Wednesday 00:15 AEST = Tuesday 14:15 UTC
    - cron: '0 20 * * 2'    # Wednesday 06:00 AEST = Tuesday 20:00 UTC
jobs:
  trigger:
    runs-on: ubuntu-latest
    steps:
      - run: |
          curl -X POST https://lazi-api.fly.dev/coles-data-v2/sync \
               -H "Content-Type: application/json"
```

This keeps `auto_stop_machines = true` (machine sleeps normally), and GitHub Actions wakes it up by calling the `/sync` endpoint. The machine auto-starts on the HTTP request. This is the **recommended** option — no idle cost, reliable timing.

---

## 5. V2.5 File Structure

No new directories needed. Replace V2 in-place or alongside:

```
api/services/special_crawler/
  coles_crawler.py              # V1 — deprecate (remove from scheduler)
  coles_crawler_v2.py           # V2 — superseded by V2.5
  coles_crawler_v2_5.py         # NEW: V2.5 with all improvements above
  woolies_crawler.py            # Unchanged
  debug_coles_crawler_v2.py     # Dev utility, keep as-is
```

`coles_crawler_v2_5.py` key class: `ColesV25Crawler` with same public interface as V2:
- `async force_sync() -> dict | None`
- `async fetch_data() -> dict | None`

The staleness `check_freshness()` function lives in:
```
api/services/freshness.py
```
Imported by `main.py` and called in each `GET /xxx-data` endpoint.

---

## 6. Implementation Checklist (V2.5)

- [ ] `api/services/special_crawler/coles_crawler_v2_5.py` — new crawler with:
  - [ ] Single persistent session across pages
  - [ ] Session warmup (homepage visit)
  - [ ] Randomised delays between pages
  - [ ] Per-page retry on block detection (2 retries, backoff 30s / 90s)
  - [ ] Expanded block signal detection
  - [ ] CSS selector fallback chains
  - [ ] Partial save logic (MIN_PRODUCTS_TO_SAVE = 50)
  - [ ] `crawl_status` / metadata in saved JSON
  - [ ] Updated StealthyFetcher settings (locale, timezone, google_search=True, timeout 60s)
- [ ] `api/services/freshness.py` — `check_freshness(data)` utility
- [ ] `api/main.py` — strip internal metadata fields in all three `GET /xxx-data` endpoints (frozen shape, no new fields added)
- [ ] `api/main.py` — extend `/health` with `data_freshness` block (the ONLY place freshness is exposed)
- [ ] `api/scheduler.py` — retire V1 job, add V2.5 job at 00:15, add conditional retry at 06:00
- [ ] `.github/workflows/crawl-trigger.yml` — GitHub Actions external cron trigger (Option B)
- [ ] Update `fly.toml` — set `min_machines_running = 0` (safe once GitHub Actions trigger is in place)

---

## 7. Future: V3 (External Services — Deferred)

If V2.5 still fails consistently, escalate to V3 which uses external services. Documented here for reference:

- **Strategy 2:** Firecrawl (`firecrawl-py`) — AI crawling service, free tier ~500 credits/month, handles Incapsula transparently. Requires `FIRECRAWL_API_KEY` secret.
- **Strategy 3:** Residential proxied Scrapling — pass `proxy=` to `StealthyFetcher`. Requires `PROXY_URL` secret from Webshare / Bright Data.
- **AI extractor fallback:** Claude Haiku to extract products from HTML when CSS selectors return 0 results. Requires `ANTHROPIC_API_KEY`.

V3 uses a strategy chain (try in order, stop at first success):
```
V2.5 logic → Firecrawl → Scrapling+Proxy → alert + keep old data
```

---

## 8. Key References

- **Scrapling docs:** https://github.com/D4Vinci/Scrapling
- **Coles specials URL:** `https://www.coles.com.au/on-special?filter_Special=halfprice`
- **Woolies specials URL (reference — working):** `https://www.woolworths.com.au/shop/browse/specials/half-price`
- **Fly.io app:** `lazi-api`, region `syd`, size `shared-cpu-2x / 4GB`
- **Storage:** Cloudflare R2, key `/home/crawlers/coles_specials.json`
- **Scheduler:** APScheduler AsyncIOScheduler, timezone `Australia/Sydney`, crawl day: Wednesday
