# ADR-004: Crawl all discounts (not just half price) + nullable `discount_type`

**Status:** Accepted
**Date:** 2026-06-13
**Relates to:** [adr-001](adr-001-scrapling-0.4-stealth-stack.md), [adr-003](adr-003-woolies-stealth-xhr-capture.md)

## Context

The crawlers previously captured only half-price (50% off) items. The product
owner asked to capture **any** discounted product (normal discounts, half
price, and more-than-half), with a new **nullable** field so the frontend can
render each tier. The existing frozen product shape must keep working; a new
nullable property is explicitly sanctioned.

## Decision

### New product field: `discount_type`

Added to every product (nullable). Computed by a shared classifier
(`services/special_crawler/discounts.py`) from `price` vs `price_was`:

| value | meaning | rule |
|---|---|---|
| `"discount"` | normal discount | fraction off < 48% |
| `"half_price"` | ~half price | 48–52% off, or retailer's explicit half-price flag |
| `"beyond_half"` | more than half off | > 52% off |
| `null` | no measurable discount | no valid was>now price |

The 48–52% band absorbs rounding so retailer "½ Price" branding lands on
`half_price` even when the arithmetic is 49% or 51%. Woolworths' `IsHalfPrice`
flag takes precedence over the band. This is backward-compatible: existing
fields are unchanged, and `main.py` already passes product fields through
untouched (only envelope-level internal metadata is stripped), so the new
field reaches the frontend automatically.

Each crawler now keeps **only products with a genuine `was > now` discount**
and tags each with `discount_type`. Items with no was-price (everyday-low /
"Down Down" / multibuy) are excluded — they aren't a quantifiable discount.

### Coles: all on-special

URL changed from `/on-special?filter_Special=halfprice` to `/on-special`
(all specials). The was>now filter naturally drops the no-was everyday-low
items. Pagination separator fixed (`?page=` vs `&page=`) now that the base URL
has no query string.

### Woolworths: multi-category grocery crawl

Woolworths has no single "all grocery discounts" grid, and the broad
`everyday-market-specials-and-offers` feed is the **MarketPlace** (third-party,
non-grocery — Lenovo servers, treadmills), so it's excluded. Investigation of
every specials category showed only two expose a was>now delta on **grocery**:

- `half-price` — ~1700 items, all 50% off
- `online-only-specials` — grocery online deals at varied % (20–50%), the
  non-half discounts the half-price feed misses

The other categories (`lower-shelf-price`, `seasonal-price`,
`everyday-low-price`, `buy-more-save-more`) have `WasPrice == Price` /
`SavingsAmount == 0` — permanent-low or multibuy programs with nothing to show.

The crawler now sweeps `["online-only-specials", "half-price"]` in one stealth
session, dedupes by product link across categories, and tags each item.
`online-only-specials` is crawled **first** (it's small) so its non-half
discounts are always represented before the wall-time budget is spent on the
much larger half-price feed.

## Consequences

- Both retailers now serve normal + half + beyond-half discounts with a
  `discount_type` the frontend can switch on; the legacy half-price-only view
  still works (filter `discount_type == "half_price"`).
- Coles gains a large set of non-half discounts (`/on-special` is far bigger
  than the half-price filter). Woolworths gains the `online-only-specials`
  non-half grocery deals; its half-price feed remains the bulk, reflecting how
  Woolworths actually brands grocery discounts (most was/now deals are ½ Price;
  the rest are permanent low prices with no was).
- Crawl wall-time bound (`MAX_CRAWL_SECONDS=360`) now applies to **both**
  crawlers (backported to Coles), since all-specials feeds are larger.
- `crawler_version` bumped: Coles `v2.6-alldiscounts`, Woolies
  `woolies-v3-alldiscounts`.
- Regression coverage: `tests/test_discounts.py` (classifier bands) plus the
  extractor tests now assert `discount_type` and the all-discounts filter.

## Coverage tuning (follow-up, 2026-06-13)

First all-discounts deploy returned fewer items than the old half-price-only
crawl (Coles 105, Woolies 140) because the 360s budget + slow per-page time on
Fly capped the page count. Tuned for more coverage without weakening stealth,
the frozen shape, or machine-sleep safety:

- **`disable_resources` was tried and reverted.** It looked safe locally
  (identical extraction) but on Fly it *halved* Coles' yield (61 vs 105) and
  burned the full budget — Coles' Incapsula challenge needs CSS/JS to
  complete, so blocking resources gets pages blocked. For Woolies it would
  also stall, since `wait_selector` waits for the rendered `<wc-product-tile>`
  which needs CSS/JS. Lesson: don't strip resources from anti-bot-protected,
  render-dependent pages. Coverage comes from time + page caps only.
- inter-page delay 3–7s → 2–4s (still randomised); settle `wait` kept at
  3000/2500ms (Coles needs it for the Incapsula JS to settle).
- `MAX_CRAWL_SECONDS` 360 → 600 — observed background crawls already ran
  15–20 min and completed on Fly, and the POST `/sync` path holds the
  connection open the whole crawl, so 10 min is safely within the window.
- Page caps raised so the wall-time budget is the real limiter: Coles
  `max_pages` 20 → 50, Woolies 12 → 30 per category.

Local scaling check: Coles 8 pages → 316 products, Woolies 6 pages/category →
387 — both linear in page count with extraction intact.
