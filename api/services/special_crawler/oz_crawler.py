from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import re
from xml.etree import ElementTree

import requests


FEED_URL = "https://www.ozbargain.com.au/deals/feed"
OZBARGAIN_META = "{https://www.ozbargain.com.au}meta"
MEDIA_THUMBNAIL = "{http://search.yahoo.com/mrss/}thumbnail"
DEFAULT_WISH_LIST = ("LEGO", "Xiaomi", "DJI", "iPhone", "Apple", "RTX")
PRICE_PATTERN = re.compile(r"\$\d[\d,]*(?:\.\d{2})?")


class OzFeedUnavailable(Exception):
    pass


def parse_deal(item, now):
    title = item.findtext("title", "").strip()
    link = item.findtext("link", "").strip()
    if not title or not link:
        return None

    meta = item.find(OZBARGAIN_META)
    thumbnail = item.find(MEDIA_THUMBNAIL)
    try:
        if meta is not None and meta.get("expiry") and int(meta.get("expiry")) <= now.timestamp():
            return None
    except ValueError:
        pass

    try:
        posted = parsedate_to_datetime(item.findtext("pubDate", ""))
    except ValueError:
        posted = None

    price = PRICE_PATTERN.search(title)
    return {
        "name": title,
        "price": price.group() if price else None,
        "link": link,
        "node_url": link,
        "image": thumbnail.get("url") if thumbnail is not None else None,
        "time": posted,
        "tag": None,
    }


class OzCrawler:
    def oz_crawl_pipeline(self, page: int, wish_list: list[str] | None = None):
        # The published RSS feed has one page and does not support pagination.
        try:
            response = requests.get(FEED_URL, timeout=10)
            response.raise_for_status()
            channel = ElementTree.fromstring(response.content).find("channel")
        except (requests.RequestException, ElementTree.ParseError) as exc:
            raise OzFeedUnavailable("Could not load OzBargain RSS") from exc

        if channel is None:
            raise OzFeedUnavailable("OzBargain returned an invalid RSS feed")

        now = datetime.now(timezone.utc)
        deals = [deal for item in channel.findall("item") if (deal := parse_deal(item, now))]
        terms = tuple(word.strip().casefold() for word in (wish_list or DEFAULT_WISH_LIST))
        matches = [deal for deal in deals if any(term in deal["name"].casefold() for term in terms)]
        return matches if wish_list or matches else deals
