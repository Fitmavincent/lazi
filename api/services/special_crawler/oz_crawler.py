from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import logging
import re
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit
from xml.etree import ElementTree

import requests


FEED_URL = "https://www.ozbargain.com.au/deals/feed"
FEED_PAGE_COUNT = 6
FEED_CACHE_SECONDS = 120
OZBARGAIN_META = "{https://www.ozbargain.com.au}meta"
MEDIA_THUMBNAIL = "{http://search.yahoo.com/mrss/}thumbnail"
DEFAULT_WISH_LIST = ("LEGO", "Xiaomi", "DJI", "iPhone", "Apple", "RTX")
PRICE_PATTERN = re.compile(r"\$(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{2})?")
logger = logging.getLogger(__name__)


class OzFeedUnavailable(Exception):
    pass


def parse_expiry(value: str) -> datetime | None:
    try:
        if value.isdecimal():
            return datetime.fromtimestamp(int(value), timezone.utc)
        expiry = datetime.fromisoformat(value)
        return expiry if expiry.tzinfo else expiry.replace(tzinfo=timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


def find_retailer_link(meta: ElementTree.Element | None) -> str | None:
    if meta is None:
        return None

    for field in ("url", "link"):
        value = meta.get(field, "").strip()
        try:
            target = urlsplit(value)
        except ValueError:
            continue
        if target.scheme not in ("http", "https") or not target.hostname:
            continue
        host = target.hostname
        is_ozbargain = host == "ozbargain.com.au" or host.endswith(".ozbargain.com.au")
        if not is_ozbargain or target.path.startswith("/goto/"):
            return value
    return None


def parse_deal(item, now):
    title = item.findtext("title", "").strip()
    link = item.findtext("link", "").strip()
    if not title or not link:
        return None

    meta = item.find(OZBARGAIN_META)
    thumbnail = item.find(MEDIA_THUMBNAIL)
    expiry = parse_expiry(meta.get("expiry", "")) if meta is not None else None
    if expiry is not None and expiry <= now:
        return None

    try:
        posted = parsedate_to_datetime(item.findtext("pubDate", ""))
    except ValueError:
        posted = None

    price = PRICE_PATTERN.search(title)
    return {
        "name": title,
        "price": price.group() if price else None,
        "link": link,
        "node_url": find_retailer_link(meta),
        "image": thumbnail.get("url") if thumbnail is not None else None,
        "time": posted,
        "tag": None,
    }


def fetch_feed_page(index: int) -> list[ElementTree.Element] | None:
    try:
        response = requests.get(FEED_URL, params={"page": index} if index else None, timeout=6)
        response.raise_for_status()
        channel = ElementTree.fromstring(response.content).find("channel")
        if channel is not None:
            return channel.findall("item")
        logger.warning("OzBargain RSS page %d is missing its channel", index)
    except (requests.RequestException, ElementTree.ParseError) as exc:
        logger.warning("Could not load OzBargain RSS page %d: %s", index, exc)
    return None


class OzCrawler:
    def __init__(self):
        self._lock = Lock()
        self._pages: list[list[ElementTree.Element] | None] = []
        self._refresh_after = 0.0

    def _feed_pages(self):
        # The default view and keyword searches share one batch of RSS pages.
        with self._lock:
            if monotonic() >= self._refresh_after:
                with ThreadPoolExecutor(max_workers=3) as pool:
                    pages = list(pool.map(fetch_feed_page, range(FEED_PAGE_COUNT)))
                if any(items is not None for items in pages):
                    self._pages = pages
                    self._refresh_after = monotonic() + FEED_CACHE_SECONDS
                return pages
            return self._pages

    def oz_crawl_pipeline(self, page: int, wish_list: list[str] | None = None):
        pages = self._feed_pages()[:max(1, page)]
        if all(items is None for items in pages):
            raise OzFeedUnavailable("Could not load OzBargain RSS")
        now = datetime.now(timezone.utc)
        deals = {}
        for items in pages:
            for item in items or ():
                deal = parse_deal(item, now)
                if deal is not None:
                    deals.setdefault(deal["link"], deal)

        terms = tuple(word.strip().casefold() for word in (wish_list or DEFAULT_WISH_LIST))
        matches = [deal for deal in deals.values() if any(term in deal["name"].casefold() for term in terms)]
        return matches if wish_list or matches else list(deals.values())
