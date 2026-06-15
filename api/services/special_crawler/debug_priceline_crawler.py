"""
Debug version of the Priceline crawler.

Reuses the production PricelineCrawler pipeline (scrapling 0.4
AsyncStealthySession + in-page OCC API fetch) but swaps R2 storage for local
files.

Run:
    cd api
    python -m services.special_crawler.debug_priceline_crawler [max_pages] [--headed]
"""

import json
import sys
import logging
import asyncio

from services.special_crawler.priceline_crawler import PricelineCrawler, ProductExtractor

logger = logging.getLogger(__name__)
OUTPUT_JSON = "priceline_specials_debug.json"


class DebugPricelineCrawler(PricelineCrawler):
    def __init__(self, max_pages: int = 40, headless: bool = True):
        self.max_pages = max_pages
        self.headless = headless
        self.extractor = ProductExtractor()
        logger.info(f"DebugPricelineCrawler initialized (max_pages={max_pages}, headless={headless})")

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
    REQUIRED_ENVELOPE = {"synced_at", "count", "data"}
    REQUIRED_PRODUCT = {
        "name", "price", "price_per_unit", "price_was",
        "product_link", "image", "discount", "retailer",
    }
    OPTIONAL_PRODUCT = {"discount_type"}
    errors = []
    if REQUIRED_ENVELOPE - data.keys():
        errors.append(f"Envelope missing: {REQUIRED_ENVELOPE - data.keys()}")
    if not isinstance(data.get("count"), int):
        errors.append("'count' must be int")
    items = data.get("data", [])
    for i, item in enumerate(items):
        missing = REQUIRED_PRODUCT - item.keys()
        if missing:
            errors.append(f"Item {i} missing: {missing}")
        extra = set(item.keys()) - REQUIRED_PRODUCT - OPTIONAL_PRODUCT
        if extra:
            errors.append(f"Item {i} extra: {extra}")
        if errors and i > 10:
            break
    if errors:
        logger.error("Validation FAILED:")
        for e in errors[:15]:
            logger.error(f"  - {e}")
        return False
    logger.info(f"Data structure validation PASSED ({len(items)} products)")
    for item in items[:3]:
        logger.info(f"  Sample: {item['name']!r} | ${item['price']} (was ${item['price_was']}) | {item['discount_type']}")
    return True


async def main():
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)-8s %(name)s — %(message)s', datefmt='%H:%M:%S')
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    max_pages = int(args[0]) if args else 40
    headless = '--headed' not in sys.argv

    crawler = DebugPricelineCrawler(max_pages=max_pages, headless=headless)
    data = await crawler.crawl_pipeline()
    ok = validate_data_structure(data)
    crawler.save_to_file(data)

    print("\n" + "=" * 60)
    print(f"  crawl_status   : {data['crawl_status']}")
    print(f"  pages_attempted: {data['pages_attempted']}")
    print(f"  products found : {data['count']}")
    print(f"  validation     : {'PASS' if ok else 'FAIL'}")
    print("=" * 60)
    if data['data']:
        from collections import Counter
        print("discount_type:", dict(Counter(p['discount_type'] for p in data['data'])))
        for p in data['data'][:3]:
            print(f"  {p['name']} | ${p['price']} (was ${p['price_was']}) | {p['discount']}")


if __name__ == "__main__":
    asyncio.run(main())
