"""Priceline extractor regression test against a saved OCC search snapshot."""

import json
import pathlib
from unittest.mock import Mock

import pytest

from services.special_crawler.priceline_crawler import ProductExtractor
from services.special_crawler.debug_priceline_crawler import DebugPricelineCrawler

SNAPSHOT = pathlib.Path(__file__).resolve().parent / "fixtures" / "priceline_search_snapshot.json"


@pytest.fixture(scope="module")
def products():
    if not SNAPSHOT.exists():
        pytest.skip("No OCC snapshot fixture — run the debug crawler to generate one")
    raw = json.loads(SNAPSHOT.read_text())
    return ProductExtractor().extract_all(raw)


def test_extracts_products(products):
    assert len(products) >= 5


def test_all_items_are_genuine_discounts(products):
    for p in products:
        assert p["price_was"] > p["price"] > 0


def test_frozen_product_shape_plus_discount_type(products):
    expected = {
        "name", "price", "price_per_unit", "price_was",
        "product_link", "image", "discount", "retailer", "discount_type",
    }
    for p in products:
        assert set(p.keys()) == expected
        assert p["retailer"] == "Priceline"


def test_discount_type_present(products):
    for p in products:
        assert p["discount_type"] in {"half_price", "beyond_half", "discount"}


def test_links_and_images_absolute(products):
    for p in products:
        assert p["product_link"].startswith("https://www.priceline.com.au/")
        if p["image"]:
            assert p["image"].startswith("https://")


def test_empty_payload_returns_empty():
    ex = ProductExtractor()
    assert ex.extract_all([]) == []
    assert ex.extract_all(None) == []


@pytest.mark.asyncio
async def test_fetches_occ_pages_without_loading_the_sale_page(monkeypatch):
    raw = json.loads(SNAPSHOT.read_text())
    calls = []

    def get(_, url, *, params, timeout):
        assert url == "https://api.priceline.com.au/occ/v2/priceline/products/search"
        calls.append(params["currentPage"])
        return Mock(json=Mock(return_value={"products": raw, "pagination": {"totalPages": 2}}))

    monkeypatch.setattr("requests.Session.get", get)
    result = await DebugPricelineCrawler(max_pages=4).crawl_pipeline()

    assert calls == [0, 1]
    assert result["pages_attempted"] == 2
    assert result["pages_succeeded"] == 2
    assert result["count"] == len(ProductExtractor().extract_all(raw))
