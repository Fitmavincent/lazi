"""
API endpoint tests with mocked R2 storage and crawlers.

R2 env vars are stubbed before importing main so Settings() resolves;
crawler fetch/sync methods are monkeypatched so no network or storage
is touched.
"""

from contextlib import nullcontext
import os
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

os.environ.setdefault("R2_ENDPOINT_URL", "https://example.invalid")
os.environ.setdefault("R2_ACCESS_KEY_ID", "test")
os.environ.setdefault("R2_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("R2_BUCKET_NAME", "test")

import pytest
from fastapi.testclient import TestClient

import main as main_module
from services import registry
from services.special_crawler.oz_crawler import FEED_PAGE_COUNT, OzCrawler

PRODUCT = {
    "name": "Test Crackers",
    "price": 2.0,
    "price_per_unit": "$0.89/ 100g",
    "price_was": 4.0,
    "product_link": "https://www.coles.com.au/product/test",
    "image": "https://www.coles.com.au/img.jpg",
    "discount": "Save $2.00",
    "retailer": "Coles",
}

FROZEN_PRODUCT_FIELDS = {
    "name", "price", "price_per_unit", "price_was",
    "product_link", "image", "discount", "retailer",
}


def fresh_envelope():
    return {
        "synced_at": datetime.now(timezone.utc).isoformat(),
        "crawl_status": "success",
        "pages_attempted": 8,
        "pages_succeeded": 8,
        "pages_blocked": 0,
        "crawler_version": "v2.5",
        "count": 1,
        "data": [PRODUCT],
    }


def stale_envelope():
    return {
        "synced_at": (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(),
        "crawl_status": "success",
        "count": 1,
        "data": [PRODUCT],
    }


@pytest.fixture
def client(monkeypatch):
    # Never let tests start real crawls or the scheduler
    monkeypatch.setattr(registry.coles_refresh, "_task", None)
    monkeypatch.setattr(registry.coles_refresh, "_last_attempt", 0.0)
    monkeypatch.setattr(registry.woolies_refresh, "_task", None)
    monkeypatch.setattr(registry.woolies_refresh, "_last_attempt", 0.0)
    with TestClient(main_module.app) as c:
        yield c


def set_coles_data(monkeypatch, envelope):
    async def fetch_data():
        return envelope
    monkeypatch.setattr(registry.coles_v2_5_crawler_service, "fetch_data", fetch_data)


@pytest.fixture
def oz_client(client, monkeypatch):
    monkeypatch.setattr(main_module, "oz_crawler_service", OzCrawler())
    return client


@pytest.fixture
def oz_page(monkeypatch):
    playwright = Mock()
    browser = playwright.firefox.launch.return_value
    page = browser.new_page.return_value
    page.goto.return_value = Mock(ok=True, status=200)
    monkeypatch.setattr("services.special_crawler.oz_crawler.sync_playwright", lambda: nullcontext(playwright))
    return page


def oz_deal_card(title, node_id, extra=""):
    return f"""
    <div class="node-ozbdeal">
      <h2 class="title" data-title="{title}"><a href="/node/{node_id}">{title}</a></h2>
      <div class="submitted">on 22/11/2026 - 09:00</div>
      {extra}
    </div>
    """


@pytest.mark.parametrize(
    ("title", "price"),
    [
        ("Pantry staples $5.50", "$5.50"),
        ("Pantry staples $197, delivered", "$197"),
        ("Pantry staples $1,299.95, delivered", "$1,299.95"),
    ],
)
def test_oz_feed_uses_latest_deals_when_no_default_keywords_match(oz_client, oz_page, title, price):
    oz_page.content.return_value = oz_deal_card(title, 123, """
    <div class="foxshot-container">
      <a href="/goto/123" title="Go to https://shop.example.com/pantry">
        <img src="https://example.com/deal.jpg" />
      </a>
    </div>
    """)

    res = oz_client.get("/oz-data")
    assert res.status_code == 200
    assert res.json() == [{
        "name": title,
        "price": price,
        "link": "https://www.ozbargain.com.au/node/123",
        "node_url": "https://shop.example.com/pantry",
        "image": "https://example.com/deal.jpg",
        "time": "2026-11-22T09:00:00+11:00",
        "tag": None,
    }]
    assert oz_client.get("/oz-data", params={"wish": "pantry"}).json() == res.json()
    assert oz_client.get("/oz-data", params={"wish": "iphone"}).json() == []


def test_oz_upstream_http_error_stops_browser_and_backs_off(oz_client, oz_page):
    oz_page.goto.return_value = Mock(ok=False, status=403)

    for _ in range(2):
        res = oz_client.get("/oz-data")
        assert res.status_code == 502
        assert res.json()["detail"] == "Ozbargain feed is unavailable"

    assert oz_page.goto.call_count == 1


def test_oz_search_includes_older_pages_without_duplicate_or_expired_deals(oz_client, oz_page):
    first_item = oz_deal_card("Monitor $399", 123)
    older_items = oz_deal_card("Monitor $249", 122, '<a href="/goto/122">Go to deal</a>') + oz_deal_card(
        "Monitor $199", 121, '<span class="tagger expired">Expired</span>'
    )
    oz_page.goto.side_effect = [Mock(ok=True), Mock(ok=True), Mock(ok=False, status=429)]
    oz_page.content.side_effect = [first_item, first_item + older_items]

    results = oz_client.get("/oz-data", params={"wish": "monitor"}).json()

    assert [(deal["price"], deal["node_url"]) for deal in results] == [
        ("$399", None),
        ("$249", "https://www.ozbargain.com.au/goto/122"),
    ]
    assert oz_client.get("/oz-data", params={"wish": "monitor", "page": 1}).json() == results[:1]
    assert [call.args[0] for call in oz_page.goto.call_args_list] == [
        "https://www.ozbargain.com.au/deals",
        "https://www.ozbargain.com.au/deals?page=1",
        "https://www.ozbargain.com.au/deals?page=2",
    ]


def test_oz_preserves_good_cache_when_a_refresh_is_blocked(oz_client, oz_page, monkeypatch):
    clock = [1000]
    monkeypatch.setattr("services.special_crawler.oz_crawler.monotonic", lambda: clock[0])
    oz_page.content.return_value = oz_deal_card("Monitor $399", 123)

    first = oz_client.get("/oz-data").json()
    assert len(first) == 1
    assert oz_page.goto.call_count == FEED_PAGE_COUNT

    clock[0] += 120
    oz_page.goto.return_value = Mock(ok=False, status=429)
    assert oz_client.get("/oz-data").json() == first
    assert oz_client.get("/oz-data").json() == first
    assert oz_page.goto.call_count == FEED_PAGE_COUNT + 1


def test_v2_5_response_shape_is_frozen(client, monkeypatch):
    set_coles_data(monkeypatch, fresh_envelope())
    res = client.get("/coles-data-v2-5")
    assert res.status_code == 200
    body = res.json()
    assert set(body.keys()) == {"synced_at", "count", "data"}
    assert set(body["data"][0].keys()) == FROZEN_PRODUCT_FIELDS


def test_fresh_data_does_not_trigger_refresh(client, monkeypatch):
    set_coles_data(monkeypatch, fresh_envelope())
    sync_calls = []

    async def fake_sync():
        sync_calls.append(1)
        return fresh_envelope()
    monkeypatch.setattr(registry.coles_refresh, "_sync_fn", fake_sync)

    res = client.get("/coles-data-v2-5")
    assert res.status_code == 200
    assert sync_calls == []
    assert registry.coles_refresh.is_running is False


def test_stale_data_triggers_background_refresh(client, monkeypatch):
    set_coles_data(monkeypatch, stale_envelope())
    sync_started = asyncio.Event()

    async def fake_sync():
        sync_started.set()
        return fresh_envelope()
    monkeypatch.setattr(registry.coles_refresh, "_sync_fn", fake_sync)

    res = client.get("/coles-data-v2-5")
    # Stale data is still returned immediately — refresh happens in background
    assert res.status_code == 200
    assert res.json()["count"] == 1
    assert registry.coles_refresh._last_attempt > 0


def test_missing_data_404_still_triggers_refresh(client, monkeypatch):
    set_coles_data(monkeypatch, None)

    res = client.get("/coles-data-v2-5")
    assert res.status_code == 404
    assert registry.coles_refresh._last_attempt > 0  # refresh was attempted


def test_repeated_stale_fetches_trigger_only_once(client, monkeypatch):
    set_coles_data(monkeypatch, stale_envelope())
    release = asyncio.Event()
    sync_calls = []

    async def slow_sync():
        sync_calls.append(1)
        await release.wait()
        return fresh_envelope()
    monkeypatch.setattr(registry.coles_refresh, "_sync_fn", slow_sync)

    client.get("/coles-data-v2-5")
    client.get("/coles-data-v2-5")
    client.get("/coles-data-v2-5")
    release.set()
    assert len(sync_calls) <= 1


def test_health_reports_freshness(client, monkeypatch):
    set_coles_data(monkeypatch, stale_envelope())

    async def woolies_fetch():
        return fresh_envelope()
    monkeypatch.setattr(registry.woolies_crawler_service, "fetch_data", woolies_fetch)

    res = client.get("/health")
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ok"
    assert body["data_freshness"]["coles"]["is_stale"] is True
    assert body["data_freshness"]["woolies"]["is_stale"] is False
    assert "refresh_in_progress" in body["data_freshness"]["coles"]


def test_woolies_endpoint_strips_internal_fields(client, monkeypatch):
    async def woolies_fetch():
        env = fresh_envelope()
        env["data"] = [dict(PRODUCT, retailer="Woolies")]
        return env
    monkeypatch.setattr(registry.woolies_crawler_service, "fetch_data", woolies_fetch)

    res = client.get("/woolies-data")
    assert res.status_code == 200
    assert set(res.json().keys()) == {"synced_at", "count", "data"}


def test_legacy_coles_endpoints_strip_internal_fields(client, monkeypatch):
    async def legacy_fetch():
        return stale_envelope()
    monkeypatch.setattr(registry.coles_crawler_service, "fetch_data", legacy_fetch)
    monkeypatch.setattr(registry.coles_v2_crawler_service, "fetch_data", legacy_fetch)

    for ep in ("/coles-data", "/coles-data-v2"):
        res = client.get(ep)
        assert res.status_code == 200
        assert set(res.json().keys()) == {"synced_at", "count", "data"}
