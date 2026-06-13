import json
import boto3
import logging
import asyncio
import random
from datetime import datetime, timezone
from scrapling.fetchers import AsyncStealthySession
from core.settings import get_settings
from services.special_crawler.discounts import classify_discount

WOOLIES_BASE_URL = "https://www.woolworths.com.au"
WOOLIES_SPECIAL_BASE = f"{WOOLIES_BASE_URL}/shop/browse/specials"

# Woolworths splits grocery specials into categories. Only these two expose a
# genuine was>now delta on grocery items:
#   - half-price          : ~1700 items, all 50% off
#   - online-only-specials: grocery online deals at varied % (the non-half
#                           discounts the half-price feed misses)
# The other categories are everyday-low / seasonal / multibuy programs with
# WasPrice == Price (nothing quantifiable), and "everyday-market-specials-and-
# offers" is the MarketPlace (third-party, non-grocery) feed — both excluded.
# online-only is crawled first (it's small) so its non-half discounts are
# always represented before the wall-time budget is spent on half-price.
WOOLIES_CATEGORIES = ["online-only-specials", "half-price"]

# The specials page renders products into shadow-DOM web components
# (<wc-product-tile>), so the HTML can't be scraped directly. Instead we run a
# stealth browser session (same engine as the Coles V2.5 crawler) and capture
# the category API JSON it fetches — clean, structured product data.
WOOLIES_XHR_PATTERN = "apis/ui/browse/category"
TILE_SELECTOR = "wc-product-tile"

BLOCK_SIGNALS = [
    "Access Denied",
    "Pardon Our Interruption",
    "Reference #",
    "challenge-platform",
    "cf-challenge",
]

MIN_PRODUCTS_TO_SAVE = 50
MIN_PRODUCTS_SUCCESS = 150
MAX_PAGE_RETRIES = 2
BLOCK_BACKOFF = [20, 45]
# Abort the crawl early after this many consecutive failed pages — once the
# session is flagged, burning through the remaining pages only wastes time.
MAX_CONSECUTIVE_FAILURES = 3
# Per-page browser timeout (ms). Successful pages render the product tiles in a
# few seconds; a blocked page never renders them, so a tight timeout lets it
# fail fast instead of stalling the whole crawl for the full default 60s.
PAGE_TIMEOUT_MS = 30000
# Hard ceiling on total crawl wall-time. The Fly machine can be stopped a few
# minutes after the triggering request goes idle, so the crawl must finish and
# save well within that window rather than grinding through every page.
MAX_CRAWL_SECONDS = 600

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_blocked(html: str) -> bool:
    return any(signal in html for signal in BLOCK_SIGNALS)


async def human_delay(min_s: float = 3.0, max_s: float = 7.0):
    await asyncio.sleep(random.uniform(min_s, max_s))


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------

class ProductExtractor:
    """Builds the frozen product shape from the Woolies category API JSON."""

    def extract_all(self, data: dict) -> list[dict]:
        if not data or not data.get("Success"):
            logger.warning("Category API payload missing or Success=False")
            return []

        products = []
        for bundle in data.get("Bundles", []):
            for p in bundle.get("Products", []):
                name = p.get("DisplayName", "")
                if not name:
                    continue
                price = float(p.get("Price") or 0.0)
                was_price = float(p.get("WasPrice") or 0.0)
                # Only keep products with a genuine was>now discount. The feed
                # also carries everyday-low items where WasPrice == Price.
                if not (was_price > price > 0):
                    continue
                is_half = bool(p.get("IsHalfPrice"))
                stockcode = p.get("Stockcode")
                products.append({
                    "name": name,
                    "price": price,
                    "price_per_unit": p.get("CupString", "") or "",
                    "price_was": was_price,
                    "product_link": f"{WOOLIES_BASE_URL}/shop/productdetails/{stockcode}" if stockcode else "",
                    "image": p.get("LargeImageFile", "") or "",
                    "discount": self._discount(price, was_price),
                    "discount_type": classify_discount(price, was_price, is_half_price=is_half),
                    "retailer": "Woolworths",
                })
        logger.info(f"Extracted {len(products)} discounted products from page")
        return products

    def _discount(self, price: float, was_price: float) -> str:
        # Mirror the Coles crawler's discount semantics for a consistent shape.
        if was_price and price and was_price > price:
            return f"Save ${was_price - price:.2f}"
        return "Half Price"


# ---------------------------------------------------------------------------
# Production crawler
# ---------------------------------------------------------------------------

class WooliesCrawler:
    def __init__(self):
        logger.info("Initializing WooliesCrawler (scrapling 0.4 / stealth XHR capture)")
        self.max_pages = 30
        self.headless = True
        self.extractor = ProductExtractor()

        settings = get_settings()
        try:
            self.s3_client = boto3.client(
                service_name='s3',
                endpoint_url=settings.R2_ENDPOINT_URL,
                aws_access_key_id=settings.R2_ACCESS_KEY_ID,
                aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
                region_name=settings.R2_REGION
            )
            self.bucket_name = settings.R2_BUCKET_NAME
            self.file_key = '/home/crawlers/woolies_specials.json'
            logger.info("S3 client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize S3 client: {e}")
            raise

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    def _new_session(self) -> AsyncStealthySession:
        """One persistent browser for the whole crawl. capture_xhr must be set
        here (it is not a per-fetch argument for sessions)."""
        return AsyncStealthySession(
            headless=self.headless,
            block_webrtc=False,
            locale="en-AU",
            timezone_id="Australia/Sydney",
            google_search=True,
            timeout=PAGE_TIMEOUT_MS,
            wait=2500,
            # NB: no network_idle — Woolies loads ad/analytics traffic that
            # rarely goes idle, so network_idle made each page wait near the
            # full timeout. wait_selector on the product tile already
            # guarantees the category XHR has fired and been captured.
            # NB: no disable_resources either — wait_selector waits for the
            # rendered <wc-product-tile>, which needs CSS/JS; blocking those
            # would make every page wait out the full timeout. Coverage comes
            # from the wall-time budget + page caps instead.
            capture_xhr=WOOLIES_XHR_PATTERN,
            retries=1,
        )

    async def _fetch(self, session: AsyncStealthySession, url: str):
        logger.info(f"Fetching: {url}")
        try:
            return await session.fetch(url, wait_selector=TILE_SELECTOR)
        except Exception as exc:
            logger.error(f"Fetch error for {url}: {exc}")
            return None

    async def _warmup(self, session: AsyncStealthySession):
        logger.info(f"Warmup: {WOOLIES_BASE_URL}")
        try:
            response = await session.fetch(WOOLIES_BASE_URL)
            if response:
                logger.info(f"Warmup OK (status={response.status})")
        except Exception as exc:
            logger.warning(f"Warmup failed (non-fatal): {exc}")
        await human_delay(2, 5)

    # ------------------------------------------------------------------
    # Page crawl with retry
    # ------------------------------------------------------------------

    @staticmethod
    def _category_json(response) -> dict | None:
        """Return the parsed category API JSON from the captured XHRs, if any."""
        xhrs = getattr(response, "captured_xhr", None) or []
        for x in reversed(xhrs):  # most recent navigation's response first
            try:
                data = x.json()
            except Exception:
                continue
            if isinstance(data, dict) and "Bundles" in data:
                return data
        return None

    async def _crawl_single_page(self, session, category: str, page_num: int) -> tuple[list[dict], bool]:
        url = f"{WOOLIES_SPECIAL_BASE}/{category}"
        if page_num > 1:
            url = f"{url}?pageNumber={page_num}"
        response = await self._fetch(session, url)
        if not response:
            return [], False

        if is_blocked(response.html_content):
            logger.warning(f"[{category}] page {page_num}: blocked by anti-bot protection")
            return [], True

        data = self._category_json(response)
        if data is None:
            logger.warning(f"[{category}] page {page_num}: no category API payload captured")
            return [], False

        products = self.extractor.extract_all(data)
        return products, False

    async def _crawl_page_with_retry(self, session, category: str, page_num: int) -> list[dict]:
        for attempt in range(MAX_PAGE_RETRIES + 1):
            products, blocked = await self._crawl_single_page(session, category, page_num)
            if products:
                return products
            if blocked:
                if attempt < MAX_PAGE_RETRIES:
                    wait = BLOCK_BACKOFF[attempt]
                    logger.warning(f"[{category}] page {page_num} blocked. Waiting {wait}s before retry {attempt + 1}.")
                    await asyncio.sleep(wait)
            else:
                if attempt < MAX_PAGE_RETRIES:
                    logger.warning(f"[{category}] page {page_num} empty (non-block). Retrying in 10s.")
                    await asyncio.sleep(10)
        return []

    # ------------------------------------------------------------------
    # Pipeline
    # ------------------------------------------------------------------

    async def crawl_pipeline(self) -> dict:
        logger.info(
            f"Starting Woolies crawl pipeline (categories={WOOLIES_CATEGORIES}, "
            f"up to {self.max_pages} pages each, single session)"
        )

        all_products: list[dict] = []
        seen_keys: set[str] = set()
        pages_succeeded = 0
        pages_blocked = 0
        pages_attempted = 0
        loop = asyncio.get_event_loop()
        deadline = loop.time() + MAX_CRAWL_SECONDS

        async with self._new_session() as session:
            await self._warmup(session)

            for category in WOOLIES_CATEGORIES:
                if loop.time() >= deadline:
                    logger.warning(f"Wall-time budget reached before category {category!r} — skipping")
                    break
                logger.info(f"=== Category: {category} ===")
                consecutive_failures = 0

                for page_num in range(1, self.max_pages + 1):
                    if loop.time() >= deadline:
                        logger.warning(
                            f"Crawl wall-time budget ({MAX_CRAWL_SECONDS}s) exceeded in {category!r} "
                            f"at page {page_num - 1} with {len(all_products)} products total"
                        )
                        break
                    pages_attempted += 1
                    logger.info(f"[{category}] page {page_num}/{self.max_pages}")
                    products = await self._crawl_page_with_retry(session, category, page_num)

                    if products:
                        new_products = []
                        for p in products:
                            key = p.get('product_link') or p.get('name')
                            if key not in seen_keys:
                                seen_keys.add(key)
                                new_products.append(p)

                        if not new_products:
                            # Woolies re-serves earlier products past the last page;
                            # a page with zero NEW products means this category ended.
                            logger.info(f"[{category}] page {page_num}: no new products — end of category")
                            break

                        all_products.extend(new_products)
                        pages_succeeded += 1
                        consecutive_failures = 0
                        logger.info(f"[{category}] page {page_num}: {len(new_products)} new. Total: {len(all_products)}")
                    else:
                        pages_blocked += 1
                        consecutive_failures += 1
                        logger.warning(f"[{category}] page {page_num}: 0 products after all retries")
                        if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                            logger.error(f"[{category}] {consecutive_failures} consecutive failed pages — next category")
                            break

                    if page_num < self.max_pages:
                        await human_delay(2, 4)

        n = len(all_products)
        if n >= MIN_PRODUCTS_SUCCESS:
            crawl_status = "success"
        elif n >= MIN_PRODUCTS_TO_SAVE:
            crawl_status = "partial"
        else:
            crawl_status = "failed"

        logger.info(f"Crawl complete: {n} products, status={crawl_status}")
        return {
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "crawl_status": crawl_status,
            "pages_attempted": pages_attempted,
            "pages_succeeded": pages_succeeded,
            "pages_blocked": pages_blocked,
            "crawler_version": "woolies-v3-alldiscounts",
            "count": n,
            "data": all_products,
        }

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    def save_to_file(self, data: dict):
        logger.info("Saving data to Cloudflare R2")
        try:
            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=self.file_key,
                Body=json.dumps(data)
            )
            logger.info(f"Data saved to R2: {self.file_key}")
        except Exception as e:
            logger.error(f"Error saving to R2: {e}")
            raise

    def load_from_file(self) -> dict | None:
        logger.info("Loading data from Cloudflare R2")
        try:
            response = self.s3_client.get_object(
                Bucket=self.bucket_name,
                Key=self.file_key
            )
            data = json.loads(response['Body'].read().decode('utf-8'))
            logger.info(f"Loaded {len(data.get('data', []))} products from R2")
            return data
        except self.s3_client.exceptions.NoSuchKey:
            logger.warning("File not found in R2")
            return None
        except Exception as e:
            logger.error(f"Error loading from R2: {e}")
            return None

    # ------------------------------------------------------------------
    # Public interface (matches the Coles V2.5 contract)
    # ------------------------------------------------------------------

    async def force_sync(self) -> dict | None:
        logger.info("Starting force_sync (Woolies)")
        try:
            data = await self.crawl_pipeline()
            if data.get('crawl_status') == 'failed':
                logger.error("Crawl status=failed; not saving to R2 to preserve existing data")
                return None
            self.save_to_file(data)
            logger.info("force_sync completed successfully")
            return data
        except Exception as e:
            logger.error(f"Error in force_sync: {e}")
            raise

    async def fetch_data(self) -> dict | None:
        return self.load_from_file()
