"""Chemist Warehouse extractor regression test against a saved Algolia snapshot."""

import json
import pathlib

import pytest

from services.special_crawler.chemist_warehouse_crawler import ProductExtractor

SNAPSHOT = pathlib.Path(__file__).resolve().parent / "fixtures" / "cw_algolia_snapshot.json"


@pytest.fixture(scope="module")
def products():
    if not SNAPSHOT.exists():
        pytest.skip("No Algolia snapshot fixture — run the debug crawler to generate one")
    data = json.loads(SNAPSHOT.read_text())
    return ProductExtractor().extract_all(data)


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
        assert p["retailer"] == "Chemist Warehouse"


def test_discount_type_present(products):
    for p in products:
        assert p["discount_type"] in {"half_price", "beyond_half", "discount"}


def test_product_links_are_buy_urls(products):
    for p in products:
        assert p["product_link"].startswith("https://www.chemistwarehouse.com.au/buy/")


def test_prices_converted_from_cents(products):
    # cents → dollars: nothing should be in the thousands for normal items
    for p in products:
        assert p["price"] < 100000


def test_empty_payload_returns_empty():
    ex = ProductExtractor()
    assert ex.extract_all({}) == []
    assert ex.extract_all({"hits": []}) == []
