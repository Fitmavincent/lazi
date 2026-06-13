"""Extractor regression test against a saved Coles specials page snapshot."""

import pathlib

import pytest
from scrapling.parser import Selector

from services.special_crawler.coles_crawler_v2_5 import ProductExtractor

SNAPSHOT = pathlib.Path(__file__).resolve().parent / "fixtures" / "coles_specials_snapshot.html"


@pytest.fixture(scope="module")
def products():
    if not SNAPSHOT.exists():
        pytest.skip("No HTML snapshot fixture — run the debug crawler to generate one")
    page = Selector(content=SNAPSHOT.read_text())
    return ProductExtractor().extract_all(page)


def test_extracts_products(products):
    assert len(products) >= 40


def test_all_have_names_and_prices(products):
    assert all(p["name"] for p in products)
    assert all(p["price"] > 0 for p in products)


def test_all_items_are_genuine_discounts(products):
    # The crawler now keeps only products with a was>now discount
    for p in products:
        assert p["price_was"] > p["price"] > 0


def test_frozen_product_shape_plus_discount_type(products):
    expected = {
        "name", "price", "price_per_unit", "price_was",
        "product_link", "image", "discount", "retailer", "discount_type",
    }
    for p in products:
        assert set(p.keys()) == expected
        assert p["retailer"] == "Coles"


def test_discount_type_values(products):
    # snapshot is the half-price page, so every item classifies as half_price
    for p in products:
        assert p["discount_type"] in {"half_price", "beyond_half", "discount"}


def test_links_and_images_absolute(products):
    for p in products:
        if p["product_link"]:
            assert p["product_link"].startswith("https://")
        if p["image"]:
            assert p["image"].startswith("https://")
