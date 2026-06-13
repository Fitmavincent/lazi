"""Woolies extractor regression test against a saved category-API JSON snapshot."""

import json
import pathlib

import pytest

from services.special_crawler.woolies_crawler import ProductExtractor

SNAPSHOT = pathlib.Path(__file__).resolve().parent / "fixtures" / "woolies_category_snapshot.json"


@pytest.fixture(scope="module")
def products():
    if not SNAPSHOT.exists():
        pytest.skip("No JSON snapshot fixture — run the debug crawler to generate one")
    data = json.loads(SNAPSHOT.read_text())
    return ProductExtractor().extract_all(data)


def test_extracts_products(products):
    assert len(products) >= 20


def test_only_half_price_items(products):
    # Extractor filters IsHalfPrice; every item should be a genuine discount
    for p in products:
        assert p["price_was"] > p["price"] > 0


def test_all_have_names_and_prices(products):
    assert all(p["name"] for p in products)
    assert all(p["price"] > 0 for p in products)
    assert all(p["price_was"] > 0 for p in products)


def test_frozen_product_shape(products):
    expected = {
        "name", "price", "price_per_unit", "price_was",
        "product_link", "image", "discount", "retailer",
    }
    for p in products:
        assert set(p.keys()) == expected
        assert p["retailer"] == "Woolworths"


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
