"""
Debug version of the Coles V2.5 crawler.

Reuses the production ColesV25Crawler crawl pipeline (scrapling 0.4
AsyncStealthySession) but swaps R2 storage for local files, so the exact
production crawl path can be exercised without credentials.

Run:
    cd api
    python -m services.special_crawler.debug_coles_crawler_v2_5 [max_pages] [--headed]
"""

import json
import sys
import logging
import asyncio

from services.special_crawler.coles_crawler_v2_5 import ColesV25Crawler, ProductExtractor

logger = logging.getLogger(__name__)

OUTPUT_JSON = "coles_specials_v2_5_debug.json"


class DebugColesV25Crawler(ColesV25Crawler):
    """Production crawl pipeline with local-file storage and optional headed mode."""

    def __init__(self, max_pages: int = 20, headless: bool = True):
        # Skip ColesV25Crawler.__init__ — no R2/settings needed for debugging.
        self.max_pages = max_pages
        self.headless = headless
        self.extractor = ProductExtractor()
        logger.info(f"DebugColesV25Crawler initialized (max_pages={max_pages}, headless={headless})")

    def save_to_file(self, data: dict):
        with open(OUTPUT_JSON, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        logger.info(f"Saved to {OUTPUT_JSON}")

    def load_from_file(self) -> dict | None:
        try:
            with open(OUTPUT_JSON, encoding='utf-8') as f:
                return json.load(f)
        except FileNotFoundError:
            return None


def validate_data_structure(data: dict) -> bool:
    """Validates output against the frozen API shape (see design doc)."""
    REQUIRED_ENVELOPE = {"synced_at", "count", "data"}
    REQUIRED_PRODUCT = {
        "name", "price", "price_per_unit", "price_was",
        "product_link", "image", "discount", "retailer",
    }
    OPTIONAL_PRODUCT = {"discount_type"}

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
        for i, item in enumerate(items):
            missing = REQUIRED_PRODUCT - item.keys()
            if missing:
                errors.append(f"Item {i} missing fields: {missing}")
            extra = set(item.keys()) - REQUIRED_PRODUCT - OPTIONAL_PRODUCT
            if extra:
                errors.append(f"Item {i} has unexpected fields: {extra}")
            if errors and i > 10:
                break
        for i, item in enumerate(items[:20]):
            if not isinstance(item.get("price"), (int, float)):
                errors.append(f"Item {i} price not numeric")
            if not isinstance(item.get("price_was"), (int, float)):
                errors.append(f"Item {i} price_was not numeric")
            for f in ("name", "price_per_unit", "product_link", "image", "discount", "retailer"):
                if not isinstance(item.get(f), str):
                    errors.append(f"Item {i} field {f!r} not a string")

    if errors:
        logger.error("Data structure validation FAILED:")
        for e in errors[:15]:
            logger.error(f"  - {e}")
        return False

    if data.get("count") != len(items):
        logger.warning(f"count={data.get('count')} != len(data)={len(items)}")

    logger.info(f"Data structure validation PASSED ({len(items)} products)")
    for item in items[:3]:
        logger.info(f"  Sample: {item['name']!r} | ${item['price']} (was ${item['price_was']}) | {item['discount']!r}")
    return True


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)-8s %(name)s — %(message)s',
        datefmt='%H:%M:%S',
    )

    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    max_pages = int(args[0]) if args else 20
    headless = '--headed' not in sys.argv

    crawler = DebugColesV25Crawler(max_pages=max_pages, headless=headless)
    data = await crawler.crawl_pipeline()

    ok = validate_data_structure(data)
    crawler.save_to_file(data)

    print("\n" + "=" * 60)
    print(f"  crawl_status   : {data['crawl_status']}")
    print(f"  pages_attempted: {data['pages_attempted']}")
    print(f"  pages_succeeded: {data['pages_succeeded']}")
    print(f"  pages_blocked  : {data['pages_blocked']}")
    print(f"  products found : {data['count']}")
    print(f"  validation     : {'PASS' if ok else 'FAIL'}")
    print("=" * 60)

    if data['data']:
        print("\nFirst 3 products:")
        for p in data['data'][:3]:
            print(f"  {p['name']} | ${p['price']} (was ${p['price_was']}) | {p['discount']}")


if __name__ == "__main__":
    asyncio.run(main())
