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


def test_was_prices_extracted(products):
    # The vast majority of half-price items must have a was-price
    with_was = [p for p in products if p["price_was"] > 0]
    assert len(with_was) >= len(products) * 0.9


def test_was_price_consistency(products):
    for p in products:
        if p["price_was"] > 0:
            assert p["price_was"] > p["price"]


def test_frozen_product_shape(products):
    expected = {
        "name", "price", "price_per_unit", "price_was",
        "product_link", "image", "discount", "retailer",
    }
    for p in products:
        assert set(p.keys()) == expected
        assert p["retailer"] == "Coles"


def test_links_and_images_absolute(products):
    for p in products:
        if p["product_link"]:
            assert p["product_link"].startswith("https://")
        if p["image"]:
            assert p["image"].startswith("https://")
