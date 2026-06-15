"""Priceline extractor regression test against a saved OCC search snapshot."""

import json
import pathlib

import pytest

from services.special_crawler.priceline_crawler import ProductExtractor

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
