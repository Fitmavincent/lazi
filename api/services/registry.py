"""
Shared service instances.

Instantiated once here so both main.py (request path) and scheduler.py
(cron path) use the same crawler objects and the same RefreshManager locks —
a fetch-triggered refresh and a cron-triggered refresh can never run
concurrently for the same retailer.

All Coles sync paths route to the V2.5 crawler; it mirrors its output to the
legacy R2 key, so /coles-data and /coles-data-v2 stay fresh from one crawl.
"""

from services.special_crawler.coles_crawler import ColesCrawler
from services.special_crawler.coles_crawler_v2 import ColesV2Crawler
from services.special_crawler.coles_crawler_v2_5 import ColesV25Crawler
from services.special_crawler.woolies_crawler import WooliesCrawler
from services.refresh_manager import RefreshManager

coles_crawler_service = ColesCrawler()
coles_v2_crawler_service = ColesV2Crawler()
coles_v2_5_crawler_service = ColesV25Crawler()
woolies_crawler_service = WooliesCrawler()

coles_refresh = RefreshManager("coles", coles_v2_5_crawler_service.force_sync)
woolies_refresh = RefreshManager("woolies", woolies_crawler_service.force_sync)
