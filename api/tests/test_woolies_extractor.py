"""Woolies extractor regression test against a saved category-API JSON snapshot."""

from contextlib import asynccontextmanager
import json
import pathlib
from unittest.mock import AsyncMock, Mock

import pytest

from services.special_crawler.woolies_crawler import ProductExtractor, WooliesCrawler

SNAPSHOT = pathlib.Path(__file__).resolve().parent / "fixtures" / "woolies_category_snapshot.json"


@pytest.fixture(scope="module")
def products():
    if not SNAPSHOT.exists():
        pytest.skip("No JSON snapshot fixture — run the debug crawler to generate one")
    data = json.loads(SNAPSHOT.read_text())
    return ProductExtractor().extract_all(data)


def test_extracts_products(products):
    assert len(products) >= 20


def test_all_items_are_genuine_discounts(products):
    # Extractor keeps only was>now items; every one is a real discount
    for p in products:
        assert p["price_was"] > p["price"] > 0


def test_all_have_names_and_prices(products):
    assert all(p["name"] for p in products)
    assert all(p["price"] > 0 for p in products)
    assert all(p["price_was"] > 0 for p in products)


def test_frozen_product_shape_plus_discount_type(products):
    expected = {
        "name", "price", "price_per_unit", "price_was",
        "product_link", "image", "discount", "retailer", "discount_type",
    }
    for p in products:
        assert set(p.keys()) == expected
        assert p["retailer"] == "Woolworths"


def test_discount_type_present(products):
    for p in products:
        assert p["discount_type"] in {"half_price", "beyond_half", "discount"}


def test_product_links_built_from_stockcode(products):
    for p in products:
        assert p["product_link"].startswith("https://www.woolworths.com.au/shop/productdetails/")


def test_discount_string_consistent_with_coles(products):
    # Same semantics as the Coles crawler: "Save $X.XX" or "Half Price"
    for p in products:
        assert p["discount"].startswith("Save $") or p["discount"] == "Half Price"


def test_empty_or_failed_payload_returns_empty():
    ex = ProductExtractor()
    assert ex.extract_all({}) == []
    assert ex.extract_all({"Success": False, "Bundles": []}) == []
    assert ex.extract_all({"Success": True, "Bundles": []}) == []


@pytest.mark.asyncio
async def test_single_half_price_page_saves_as_partial():
    crawler = WooliesCrawler.__new__(WooliesCrawler)
    crawler.max_pages = 1
    crawler._warmup = AsyncMock()
    crawler.save_to_file = Mock()
    visited = []

    @asynccontextmanager
    async def session():
        yield object()

    async def crawl_page(_, category, page_num):
        visited.append((category, page_num))
        if category == "half-price":
            return [{"name": f"Deal {i}"} for i in range(43)]
        return []

    crawler._new_session = session
    crawler._crawl_page_with_retry = crawl_page

    result = await crawler.force_sync()

    assert visited == [("half-price", 1), ("online-only-specials", 1)]
    assert result["count"] == 43
    assert result["crawl_status"] == "partial"
    crawler.save_to_file.assert_called_once_with(result)
