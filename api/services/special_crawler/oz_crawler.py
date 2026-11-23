from datetime import datetime
import logging
import re
from threading import Lock
from time import monotonic
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup
from playwright.sync_api import Error as PlaywrightError, sync_playwright

from services.freshness import SYDNEY_TZ


BASE_URL = "https://www.ozbargain.com.au"
DEALS_URL = BASE_URL + "/deals"
FEED_PAGE_COUNT = 6
FEED_CACHE_SECONDS = 120
FAILED_REFRESH_SECONDS = 300
MAX_STALE_SECONDS = 1800
CRAWL_TIMEOUT_SECONDS = 24
PAGE_TIMEOUT_MS = 7000
DEFAULT_WISH_LIST = ("LEGO", "Xiaomi", "DJI", "iPhone", "Apple", "RTX")
PRICE_PATTERN = re.compile(r"\$(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{2})?")
POST_TIME_PATTERN = re.compile(r"\d{1,2}/\d{1,2}/\d{4} - \d{2}:\d{2}")
logger = logging.getLogger(__name__)


class OzFeedUnavailable(Exception):
    pass


def find_retailer_link(card) -> str | None:
    offer = card.select_one('a[href^="/goto/"]')
    if offer is None:
        return None

    label = offer.get("title", "")
    if label.startswith("Go to "):
        value = label.removeprefix("Go to ").strip()
        try:
            target = urlsplit(value)
            host = target.hostname
        except ValueError:
            host = None
        if host and target.scheme in ("http", "https"):
            if host == "ozbargain.com.au" or host.endswith(".ozbargain.com.au"):
                return None
            return value
    return urljoin(BASE_URL, offer["href"])


def parse_deal(card):
    if card.select_one(".tagger.expired"):
        return None
    heading = card.select_one("h2.title")
    discussion = heading.select_one('a[href^="/node/"]') if heading else None
    if discussion is None:
        return None
    title = (heading.get("data-title") or discussion.get_text(" ", strip=True)).strip()
    if not title:
        return None

    time_node = card.select_one(".submitted")
    match = POST_TIME_PATTERN.search(time_node.get_text(" ", strip=True)) if time_node else None
    try:
        posted = datetime.strptime(match.group(), "%d/%m/%Y - %H:%M").replace(tzinfo=SYDNEY_TZ) if match else None
    except ValueError:
        posted = None

    price = PRICE_PATTERN.search(title)
    thumbnail = card.select_one(".foxshot-container img[src]")
    return {
        "name": title,
        "price": price.group() if price else None,
        "link": urljoin(BASE_URL, discussion["href"]),
        "node_url": find_retailer_link(card),
        "image": urljoin(BASE_URL, thumbnail["src"]) if thumbnail else None,
        "time": posted,
        "tag": None,
    }


def crawl_feed_pages() -> list[list[dict]]:
    pages = []
    deadline = monotonic() + CRAWL_TIMEOUT_SECONDS
    try:
        with sync_playwright() as playwright:
            browser = playwright.firefox.launch(headless=True, timeout=PAGE_TIMEOUT_MS)
            try:
                page = browser.new_page()
                for index in range(FEED_PAGE_COUNT):
                    remaining_ms = int((deadline - monotonic()) * 1000)
                    if remaining_ms <= 0:
                        break
                    url = f"{DEALS_URL}?page={index}" if index else DEALS_URL
                    response = page.goto(
                        url, wait_until="domcontentloaded", timeout=min(PAGE_TIMEOUT_MS, remaining_ms)
                    )
                    if response is None or not response.ok:
                        logger.warning("OzBargain page %d returned HTTP %s", index, response.status if response else None)
                        break
                    cards = BeautifulSoup(page.content(), "html.parser").select(".node-ozbdeal")
                    if not cards:
                        logger.warning("OzBargain page %d returned no deal cards", index)
                        break
                    pages.append([deal for card in cards if (deal := parse_deal(card)) is not None])
            finally:
                browser.close()
    except PlaywrightError as exc:
        logger.warning("OzBargain browser crawl stopped after %d pages: %s", len(pages), exc)
    return pages


class OzCrawler:
    def __init__(self):
        self._lock = Lock()
        self._pages: list[list[dict]] = []
        self._refresh_after = 0.0
        self._stale_after = 0.0

    def _feed_pages(self) -> list[list[dict]]:
        with self._lock:
            if monotonic() >= self._refresh_after:
                pages = crawl_feed_pages()
                now = monotonic()
                complete = len(pages) == FEED_PAGE_COUNT
                if pages and (len(pages) >= len(self._pages) or now >= self._stale_after):
                    self._pages = pages
                    self._stale_after = now + MAX_STALE_SECONDS
                self._refresh_after = now + (FEED_CACHE_SECONDS if complete else FAILED_REFRESH_SECONDS)
            return self._pages if monotonic() < self._stale_after else []

    def oz_crawl_pipeline(self, page: int, wish_list: list[str] | None = None):
        pages = self._feed_pages()[:max(1, page)]
        if not pages:
            raise OzFeedUnavailable("Could not load the OzBargain deal listings")
        deals = {}
        for items in pages:
            for deal in items:
                deals.setdefault(deal["link"], deal)

        terms = tuple(word.strip().casefold() for word in (wish_list or DEFAULT_WISH_LIST))
        matches = [deal for deal in deals.values() if any(term in deal["name"].casefold() for term in terms)]
        return matches if wish_list or matches else list(deals.values())
