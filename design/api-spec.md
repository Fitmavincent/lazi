# Lazi API — Specials/Discounts Endpoints (Frontend Spec)

**Base URL:** `https://lazi-api.fly.dev`
**Status:** all endpoints live (verified 2026-06-16). Read-only `GET` for the frontend.
**Last verified counts:** Coles 145 · Woolworths 213 · Chemist Warehouse 298 · Priceline 1210.

---

## 1. Product data endpoints (what the frontend consumes)

One endpoint per retailer. **All four return the exact same shape.**

| Retailer | Endpoint | Method |
|---|---|---|
| Coles | `GET /coles-data-v2-5` | GET |
| Woolworths | `GET /woolies-data` | GET |
| Chemist Warehouse | `GET /chemist-warehouse-data` | GET |
| Priceline | `GET /priceline-data` | GET |

> Coles also has `GET /coles-data` and `GET /coles-data-v2` — legacy aliases that
> return the **same data and shape** (kept for backward-compat). Use
> `/coles-data-v2-5` for new work.

### Response envelope

```jsonc
{
  "synced_at": "2026-06-13T12:32:10.159613+00:00", // ISO-8601 UTC; when this data was crawled
  "count": 145,                                      // integer; == data.length
  "data": [ /* Product[] */ ]
}
```

### Product object

Every product in `data[]` has exactly these 9 fields:

| Field | Type | Notes |
|---|---|---|
| `name` | string | Product display name |
| `price` | number | Current/sale price in AUD dollars (e.g. `11.95`) |
| `price_was` | number | Original price in AUD dollars; always `> price` |
| `discount` | string | Human label, e.g. `"Save $3.00"` |
| `discount_type` | string \| null | **Discount tier — see below.** Render badges off this |
| `price_per_unit` | string | Unit price, e.g. `"$5.98/ 100g"`. **May be empty `""`** (always empty for Chemist Warehouse & Priceline) |
| `product_link` | string | Absolute URL to the product page on the retailer site |
| `image` | string | Absolute image URL (may rarely be empty `""`) |
| `retailer` | string | `"Coles"` \| `"Woolworths"` \| `"Chemist Warehouse"` \| `"Priceline"` |

### `discount_type` enum (the field to render tiers from)

| Value | Meaning | Rule |
|---|---|---|
| `"half_price"` | ~50% off | 48–52% off (or retailer half-price flag) |
| `"beyond_half"` | more than half off | > 52% off |
| `"discount"` | normal discount | < 48% off |
| `null` | not classifiable | no measurable was/now (rare; all current items are non-null) |

All items in the feed are genuine discounts (`price_was > price`).

### Example product (real, from `/coles-data-v2-5`)

```json
{
  "name": "Gevity Rx Bone Broth Body Glue Natural Go Pack",
  "price": 11.95,
  "price_was": 14.95,
  "discount": "Save $3.00",
  "discount_type": "discount",
  "price_per_unit": "$5.98/ 100g",
  "product_link": "https://www.coles.com.au/product/gevity-rx-bone-broth-body-glue-natural-go-pack-200g-7909900",
  "image": "https://www.coles.com.au/_next/image?url=...&w=256&q=90",
  "retailer": "Coles"
}
```

### Errors

- `404 {"detail": "No data available"}` — no data stored yet for that retailer
  (shouldn't happen now; all are populated). Treat as "no specials available".

---

## 2. `GET /health` — status + freshness (monitoring, optional for UI)

Not required to render products, but useful for a "data last updated" label or a
staleness banner.

```jsonc
{
  "status": "ok",
  "data_freshness": {
    "coles":  { /* FreshnessBlock */ },
    "woolies": { /* FreshnessBlock */ },
    "chemist_warehouse": { /* FreshnessBlock */ },
    "priceline": { /* FreshnessBlock */ }
  }
}
```

**FreshnessBlock:**

| Field | Type | Notes |
|---|---|---|
| `synced_at` | string \| null | ISO-8601 UTC of stored data |
| `is_stale` | boolean | true if data predates the most recent Wed 00:00 AEST specials reset |
| `data_age_hours` | number \| null | Age in hours |
| `stale_reason` | string \| null | Explanation when stale |
| `crawl_status` | string | `"success"` \| `"partial"` \| `"failed"` (last crawl) |
| `refresh_in_progress` | boolean | A background re-crawl is currently running |
| `last_attempt_age_seconds` | number \| null | Seconds since last refresh attempt |
| `cooldown_seconds` | number | Min gap between refresh attempts (1800) |

---

## 3. Behaviour the frontend should know

- **Cold start (important).** The server runs on Fly with auto-stop; if it's
  been idle it sleeps. The **first request after idle wakes it** and may take a
  few seconds (or briefly fail). Frontend should use a generous timeout
  (~15–30s) and retry once on the first load.
- **Self-refreshing data.** A `GET` on a data endpoint returns the stored data
  immediately. If that data is stale (older than the most recent Wednesday
  00:00 AEST reset — when AU supermarket specials roll over), it *also* kicks
  off a background re-crawl. The current response is unaffected; the next fetch
  (minutes later) gets fresh data. The frontend does not need to do anything
  special — just fetch normally.
- **CORS allowlist.** The API currently allows these origins only:
  `https://vin-channel.netlify.app`, `https://home.fitmavincent.dev`,
  `http://localhost:3000`. A new frontend origin must be added to the API's
  CORS config (`api/main.py`) before browser calls will work.
- **No auth** on the read endpoints.
- **`POST /<retailer>-data/sync`** endpoints exist but force a full live crawl
  (slow, 1–10 min) — **do not call these from the frontend.** They're for ops.

---

## 4. Quick reference — all GET endpoints

```
GET /                          # {"message": "..."} liveness
GET /health                    # status + per-retailer freshness
GET /coles-data-v2-5           # Coles specials  (use this for Coles)
GET /coles-data                # Coles (legacy alias, same shape)
GET /coles-data-v2             # Coles (legacy alias, same shape)
GET /woolies-data              # Woolworths specials
GET /chemist-warehouse-data    # Chemist Warehouse specials
GET /priceline-data            # Priceline specials
```

A frontend that fetches the four retailer endpoints and renders `data[]` with
`discount_type`-driven badges has everything it needs.
