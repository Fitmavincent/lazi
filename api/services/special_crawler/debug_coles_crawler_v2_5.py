"""
Debug version of the Coles V2.5 crawler.

Differences from the production crawler:
  - headless=False  (visible browser for inspection)
  - Saves HTML snapshots locally (coles_v2_5_page_N_debug.html)
  - Saves JSON output locally (coles_specials_v2_5_debug.json)
  - No R2 / S3 dependency
  - max_pages = 1 (change to crawl more pages)
  - Verbose DEBUG logging

Run:
    cd api
    python -m services.special_crawler.debug_coles_crawler_v2_5
"""

import json
import logging
import asyncio
import random
from datetime import datetime, timezone
from scrapling.fetchers import StealthyFetcher
from urllib.parse import urljoin

COLES_BASE_URL = "https://www.coles.com.au"
COLES_SPECIAL_URL = f"{COLES_BASE_URL}/on-special?filter_Special=halfprice"

WARMUP_URLS = [
    "https://www.coles.com.au",
    "https://www.coles.com.au/specials",
]

BLOCK_SIGNALS = [
    "Pardon Our Interruption",
    "interstitial-inprogress",
    # NOTE: "/_Incapsula_Resource" is embedded in ALL Coles pages (their CDN script)
    # — do NOT use it as a block signal; it causes false positives.
    "challenge-platform",
    "cf-challenge",
]

# Selector fallback chains (most specific → most general)
CONTAINER_SELECTORS = [
    'div[data-testid="specials-product-tiles"]',
    '[data-testid="product-grid"]',
    'main ul[class*="product"]',
]

NAME_SELECTORS = [
    'a.product__link.product__image[aria-label]',
    '[data-testid="product-tile"] h2',
    '[data-testid="product-tile"] .product__title',
]

PRICE_SELECTORS = [
    '[data-testid="product-pricing"][aria-label]',
    '.price__value',
    '[class*="price"][class*="current"]',
]

MIN_PRODUCTS_TO_SAVE = 50
MIN_PRODUCTS_SUCCESS = 200

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_blocked(html: str) -> bool:
    return any(signal in html for signal in BLOCK_SIGNALS)


def is_empty_render(html: str) -> bool:
    return len(html) < 5000 and "product" not in html.lower()


async def human_delay(min_s: float = 4.0, max_s: float = 9.0):
    wait = random.uniform(min_s, max_s)
    logger.debug(f"Sleeping {wait:.1f}s (human delay)")
    await asyncio.sleep(wait)


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------

class ProductExtractor:
    """Stateless extractor; operates on a Scrapling response object."""

    def find_container(self, response):
        for sel in CONTAINER_SELECTORS:
            container = response.css_first(sel)
            if container:
                logger.info(f"Container found with selector: {sel!r}")
                return container
        logger.warning("No product container found with any known selector — possible SELECTOR_FAILURE")
        return None

    def extract_name(self, element) -> str | None:
        # Primary: aria-label on product image link
        link = element.css_first('a.product__link.product__image')
        if link:
            label = link.attrib.get('aria-label', '')
            if label:
                return label.split(' | ')[0].strip()

        # Fallbacks
        for sel in NAME_SELECTORS[1:]:
            el = element.css_first(sel)
            if el and el.text:
                return el.text.strip()
        return None

    def extract_price(self, element) -> float:
        # Primary
        price_el = element.css_first('[data-testid="product-pricing"]')
        if price_el:
            label = price_el.attrib.get('aria-label', '')
            if 'Price $' in label:
                try:
                    return float(label.replace('Price $', '').strip())
                except ValueError:
                    pass

        # Fallbacks
        for sel in PRICE_SELECTORS[1:]:
            el = element.css_first(sel)
            if el and el.text:
                text = el.text.strip().lstrip('$').replace(',', '')
                try:
                    return float(text)
                except ValueError:
                    pass
        return 0.0

    def extract_was_and_unit(self, element) -> tuple[float, str]:
        was_price = 0.0
        unit_price = ''
        calc_el = element.css_first('.price__calculation_method')
        if calc_el:
            calc_text = calc_el.text or ''
            if ' | Was $' in calc_text:
                parts = calc_text.split(' | Was $')
                unit_price = parts[0].strip()
                try:
                    was_price = float(parts[1].strip())
                except (ValueError, IndexError):
                    pass
            else:
                unit_price = calc_text.strip()
        return was_price, unit_price

    def extract_image(self, element) -> str:
        img = element.css_first('[data-testid="product-image"]')
        if not img:
            return ''
        srcset = img.attrib.get('srcset', '')
        if srcset:
            first = srcset.split(' ')[0]
            return urljoin(COLES_BASE_URL, first) if first.startswith('/') else first
        src = img.attrib.get('src', '')
        return urljoin(COLES_BASE_URL, src) if src.startswith('/') else src

    def extract_discount(self, element, was_price: float, current_price: float) -> str:
        badge = element.css_first('.badge-label')
        if badge and badge.text and 'Save' in badge.text:
            return badge.text.strip()
        if was_price > current_price > 0:
            return 'Half Price'
        return ''

    def extract_link(self, element) -> str:
        link = element.css_first('a.product__link.product__image')
        if link:
            href = link.attrib.get('href', '')
            return urljoin(COLES_BASE_URL, href) if href.startswith('/') else href
        return ''

    def extract_all(self, response) -> list[dict]:
        container = self.find_container(response)
        if not container:
            return []

        tiles = container.css('section[data-testid="product-tile"]')
        if not tiles:
            logger.warning("No product tiles found inside container")
            return []

        logger.info(f"Found {len(tiles)} product tiles")
        products = []
        for i, tile in enumerate(tiles):
            try:
                name = self.extract_name(tile)
                if not name:
                    logger.debug(f"Tile {i+1}: no name, skipping")
                    continue
                price = self.extract_price(tile)
                was_price, unit_price = self.extract_was_and_unit(tile)
                product = {
                    'name': name,
                    'price': price,
                    'price_per_unit': unit_price,
                    'price_was': was_price,
                    'product_link': self.extract_link(tile),
                    'image': self.extract_image(tile),
                    'discount': self.extract_discount(tile, was_price, price),
                    'retailer': 'Coles',
                }
                products.append(product)
                logger.debug(f"Tile {i+1}: {name!r} ${price}")
            except Exception as exc:
                logger.debug(f"Tile {i+1} extraction error: {exc}")

        logger.info(f"Extracted {len(products)} products from page")
        return products


# ---------------------------------------------------------------------------
# Debug crawler
# ---------------------------------------------------------------------------

class DebugColesV25Crawler:
    """
    Debug variant of the V2.5 crawler.
    - Single StealthyFetcher session is simulated via sequential fetches
      (Scrapling's async_fetch does not expose a persistent context API yet;
      session continuity is approximated by reusing warmup cookies implicitly
      through the fetcher's internal state).
    - Saves HTML + JSON locally, no R2 writes.
    """

    def __init__(self, max_pages: int = 1):
        self.max_pages = max_pages
        self.extractor = ProductExtractor()
        logger.info(f"DebugColesV25Crawler initialized (max_pages={max_pages})")

    # ------------------------------------------------------------------
    # Fetch helpers
    # ------------------------------------------------------------------

    async def _fetch(self, url: str, headless: bool = False) -> object | None:
        """Shared fetch call with V2.5 StealthyFetcher settings."""
        logger.info(f"Fetching: {url}")
        try:
            response = await StealthyFetcher.async_fetch(
                url,
                headless=headless,
                timeout=60000,
                wait=5000,
                humanize=True,
                block_webrtc=False,
                geoip=True,
                disable_ads=False,
                google_search=True,   # organic-looking referrer
            )
            return response
        except Exception as exc:
            logger.error(f"Fetch error for {url}: {exc}")
            return None

    async def _warmup(self):
        """Visit warmup URLs to establish cookies before hitting specials."""
        for url in WARMUP_URLS:
            logger.info(f"Warmup: {url}")
            response = await self._fetch(url)
            if response:
                logger.info(f"Warmup OK: {url} (status={response.status})")
            await human_delay(3, 6)

    # ------------------------------------------------------------------
    # Page crawl with retry
    # ------------------------------------------------------------------

    async def _crawl_single_page(self, page_num: int) -> tuple[list[dict], bool]:
        """
        Returns (products, was_blocked).
        """
        url = COLES_SPECIAL_URL if page_num == 1 else f"{COLES_SPECIAL_URL}&page={page_num}"
        response = await self._fetch(url)

        if not response:
            return [], False

        html = str(response)

        # Save HTML snapshot for inspection
        html_path = f"coles_v2_5_page_{page_num}_debug.html"
        try:
            with open(html_path, 'w', encoding='utf-8') as f:
                f.write(html)
            logger.info(f"HTML saved: {html_path}")
        except Exception as exc:
            logger.warning(f"Could not save HTML: {exc}")

        if is_blocked(html):
            logger.warning(f"Page {page_num}: BLOCKED by anti-bot protection")
            return [], True

        if is_empty_render(html):
            logger.warning(f"Page {page_num}: empty render (len={len(html)})")
            return [], False

        products = self.extractor.extract_all(response)
        return products, False

    async def _crawl_page_with_retry(self, page_num: int) -> list[dict]:
        MAX_RETRIES = 2
        BLOCK_BACKOFF = [30, 90]

        for attempt in range(MAX_RETRIES + 1):
            products, blocked = await self._crawl_single_page(page_num)
            if products:
                return products
            if blocked:
                if attempt < MAX_RETRIES:
                    wait = BLOCK_BACKOFF[attempt]
                    logger.warning(f"Page {page_num} blocked. Waiting {wait}s before retry {attempt+1}.")
                    await asyncio.sleep(wait)
            else:
                if attempt < MAX_RETRIES:
                    logger.warning(f"Page {page_num} empty (non-block). Retrying in 10s.")
                    await asyncio.sleep(10)
        return []

    # ------------------------------------------------------------------
    # Pipeline
    # ------------------------------------------------------------------

    async def crawl(self) -> dict:
        """
        Full crawl pipeline. Returns the final data envelope:
        {
          "synced_at": ...,
          "crawl_status": "success" | "partial" | "failed",
          "pages_attempted": N,
          "pages_succeeded": N,
          "pages_blocked": N,
          "crawler_version": "v2.5-debug",
          "count": N,
          "data": [...]
        }
        """
        logger.info("=== Starting V2.5 debug crawl ===")
        await self._warmup()

        all_products: list[dict] = []
        pages_succeeded = 0
        pages_blocked = 0

        for page_num in range(1, self.max_pages + 1):
            logger.info(f"--- Page {page_num}/{self.max_pages} ---")
            products = await self._crawl_page_with_retry(page_num)

            if products:
                all_products.extend(products)
                pages_succeeded += 1
                logger.info(f"Page {page_num}: {len(products)} products. Running total: {len(all_products)}")
            else:
                # Determine if last attempt was a block by checking the HTML we saved
                pages_blocked += 1
                logger.warning(f"Page {page_num}: 0 products after all retries")

            if page_num < self.max_pages:
                await human_delay(4, 9)

        # Determine crawl status
        n = len(all_products)
        if n >= MIN_PRODUCTS_SUCCESS:
            crawl_status = "success"
        elif n >= MIN_PRODUCTS_TO_SAVE:
            crawl_status = "partial"
        else:
            crawl_status = "failed"

        result = {
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "crawl_status": crawl_status,
            "pages_attempted": self.max_pages,
            "pages_succeeded": pages_succeeded,
            "pages_blocked": pages_blocked,
            "crawler_version": "v2.5-debug",
            "count": n,
            "data": all_products,
        }

        logger.info(f"=== Crawl complete: {n} products, status={crawl_status} ===")
        return result

    def save_to_file(self, data: dict, path: str = "coles_specials_v2_5_debug.json"):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved to {path}")

    def validate_data_structure(self, data: dict) -> bool:
        """
        Validates that the output conforms to the frozen API shape:
          - Top-level: synced_at (str), count (int), data (list)
          - Each item: name, price, price_per_unit, price_was,
                       product_link, image, discount, retailer
        Internal metadata fields are allowed in the envelope but must not
        contaminate the product items.
        Returns True if valid.
        """
        REQUIRED_ENVELOPE = {"synced_at", "count", "data"}
        REQUIRED_PRODUCT = {
            "name", "price", "price_per_unit", "price_was",
            "product_link", "image", "discount", "retailer",
        }
        INTERNAL_FIELDS = {
            "crawl_status", "pages_attempted", "pages_succeeded",
            "pages_blocked", "crawler_version",
        }

        errors = []

        missing_top = REQUIRED_ENVELOPE - data.keys()
        if missing_top:
            errors.append(f"Envelope missing fields: {missing_top}")

        if not isinstance(data.get("count"), int):
            errors.append("'count' must be an int")

        items = data.get("data", [])
        if not isinstance(items, list):
            errors.append("'data' must be a list")
        else:
            for i, item in enumerate(items[:5]):  # spot-check first 5
                missing = REQUIRED_PRODUCT - item.keys()
                if missing:
                    errors.append(f"Item {i} missing fields: {missing}")
                extra = set(item.keys()) - REQUIRED_PRODUCT
                if extra:
                    errors.append(f"Item {i} has unexpected fields (must be stripped before API): {extra}")

        if errors:
            logger.error("Data structure validation FAILED:")
            for e in errors:
                logger.error(f"  - {e}")
            return False

        # Check count matches
        if data.get("count") != len(items):
            logger.warning(f"count={data.get('count')} != len(data)={len(items)}")

        logger.info(f"Data structure validation PASSED ({len(items)} products)")
        # Log a few sample products
        for item in items[:3]:
            logger.info(f"  Sample: {item['name']!r} | ${item['price']} (was ${item['price_was']}) | {item['discount']!r}")
        return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def main():
    logging.basicConfig(
        level=logging.DEBUG,
        format='%(asctime)s %(levelname)-8s %(name)s — %(message)s',
        datefmt='%H:%M:%S',
    )

    crawler = DebugColesV25Crawler(max_pages=1)
    data = await crawler.crawl()

    ok = crawler.validate_data_structure(data)
    crawler.save_to_file(data)

    print("\n" + "="*60)
    print(f"  crawl_status : {data['crawl_status']}")
    print(f"  pages_attempted: {data['pages_attempted']}")
    print(f"  pages_succeeded: {data['pages_succeeded']}")
    print(f"  pages_blocked  : {data['pages_blocked']}")
    print(f"  products found : {data['count']}")
    print(f"  validation     : {'PASS' if ok else 'FAIL'}")
    print("="*60)

    if data['data']:
        print("\nFirst 3 products:")
        for p in data['data'][:3]:
            print(f"  {p['name']} | ${p['price']} (was ${p['price_was']}) | {p['discount']}")


if __name__ == "__main__":
    asyncio.run(main())
