"""
API endpoint tests with mocked R2 storage and crawlers.

R2 env vars are stubbed before importing main so Settings() resolves;
crawler fetch/sync methods are monkeypatched so no network or storage
is touched.
"""

import os
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

os.environ.setdefault("R2_ENDPOINT_URL", "https://example.invalid")
os.environ.setdefault("R2_ACCESS_KEY_ID", "test")
os.environ.setdefault("R2_SECRET_ACCESS_KEY", "test")
os.environ.setdefault("R2_BUCKET_NAME", "test")

import pytest
import requests
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


@pytest.mark.parametrize(
    ("title", "price"),
    [
        ("Pantry staples $5.50", "$5.50"),
        ("Pantry staples $197, delivered", "$197"),
        ("Pantry staples $1,299.95, delivered", "$1,299.95"),
    ],
)
def test_oz_feed_uses_latest_deals_when_no_default_keywords_match(oz_client, monkeypatch, title, price):
    response = Mock(content=f"""
    <rss xmlns:media="http://search.yahoo.com/mrss/" xmlns:oz="https://www.ozbargain.com.au"><channel><item>
      <title>{title}</title>
      <link>https://www.ozbargain.com.au/node/123</link>
      <pubDate>Sun, 22 Nov 2026 09:00:00 +1100</pubDate>
      <media:thumbnail url="https://example.com/deal.jpg" />
      <oz:meta url="https://shop.example.com/pantry" link="https://www.ozbargain.com.au/goto/123" />
    </item></channel></rss>
    """.encode())
    monkeypatch.setattr("services.special_crawler.oz_crawler.requests.get", Mock(return_value=response))

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


def test_oz_upstream_http_error_is_reported(oz_client, monkeypatch):
    response = Mock()
    response.raise_for_status.side_effect = requests.HTTPError("403 Forbidden")
    monkeypatch.setattr("services.special_crawler.oz_crawler.requests.get", Mock(return_value=response))

    res = oz_client.get("/oz-data")
    assert res.status_code == 502
    assert res.json()["detail"] == "Ozbargain feed is unavailable"


def test_oz_search_includes_older_pages_without_duplicate_or_expired_deals(oz_client, monkeypatch):
    calls = []
    first_item = """
    <item><title>Monitor $399</title><link>https://www.ozbargain.com.au/node/123</link></item>
    """
    older_items = """
    <item>
      <title>Monitor $249</title><link>https://www.ozbargain.com.au/node/122</link>
      <oz:meta link="https://www.ozbargain.com.au/goto/122" />
    </item>
    <item>
      <title>Monitor $199</title><link>https://www.ozbargain.com.au/node/121</link>
      <oz:meta expiry="2020-11-22T00:00:00+11:00" />
    </item>
    """
    pages = {0: first_item, 1: first_item + older_items}

    def fetch_page(url, *, params, timeout):
        index = params["page"] if params else 0
        calls.append(index)
        if index == 2:
            raise requests.Timeout()
        items = pages.get(index, "")
        return Mock(content=f'<rss xmlns:oz="https://www.ozbargain.com.au"><channel>{items}</channel></rss>'.encode())

    monkeypatch.setattr("services.special_crawler.oz_crawler.requests.get", fetch_page)
    results = oz_client.get("/oz-data", params={"wish": "monitor"}).json()
    assert [(deal["price"], deal["node_url"]) for deal in results] == [
        ("$399", None),
        ("$249", "https://www.ozbargain.com.au/goto/122"),
    ]
    assert oz_client.get("/oz-data", params={"wish": "monitor", "page": 1}).json() == results[:1]
    assert sorted(calls) == list(range(FEED_PAGE_COUNT))


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
