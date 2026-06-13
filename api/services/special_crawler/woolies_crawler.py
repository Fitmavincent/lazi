import json
import boto3
import logging
import asyncio
import random
from datetime import datetime, timezone
from scrapling.fetchers import AsyncStealthySession
from core.settings import get_settings

WOOLIES_BASE_URL = "https://www.woolworths.com.au"
WOOLIES_SPECIAL_URL = f"{WOOLIES_BASE_URL}/shop/browse/specials/half-price"

# The half-price specials page renders products into shadow-DOM web components
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
BLOCK_BACKOFF = [30, 90]
# Abort the crawl early after this many consecutive failed pages — once the
# session is flagged, burning through the remaining pages only wastes time.
MAX_CONSECUTIVE_FAILURES = 3

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
                if not p.get("IsHalfPrice"):
                    continue
                name = p.get("DisplayName", "")
                if not name:
                    continue
                price = p.get("Price") or 0.0
                was_price = p.get("WasPrice") or 0.0
                stockcode = p.get("Stockcode")
                products.append({
                    "name": name,
                    "price": float(price),
                    "price_per_unit": p.get("CupString", "") or "",
                    "price_was": float(was_price),
                    "product_link": f"{WOOLIES_BASE_URL}/shop/productdetails/{stockcode}" if stockcode else "",
                    "image": p.get("LargeImageFile", "") or "",
                    "discount": self._discount(price, was_price),
                    "retailer": "Woolworths",
                })
        logger.info(f"Extracted {len(products)} half-price products from page")
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
        self.max_pages = 20
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
            timeout=60000,
            wait=2500,
            # NB: no network_idle — Woolies loads ad/analytics traffic that
            # rarely goes idle, so network_idle made each page wait near the
            # full 60s timeout. wait_selector on the product tile already
            # guarantees the category XHR has fired and been captured.
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

    async def _crawl_single_page(self, session, page_num: int) -> tuple[list[dict], bool]:
        url = WOOLIES_SPECIAL_URL if page_num == 1 else f"{WOOLIES_SPECIAL_URL}?pageNumber={page_num}"
        response = await self._fetch(session, url)
        if not response:
            return [], False

        if is_blocked(response.html_content):
            logger.warning(f"Page {page_num}: blocked by anti-bot protection")
            return [], True

        data = self._category_json(response)
        if data is None:
            logger.warning(f"Page {page_num}: no category API payload captured")
            return [], False

        products = self.extractor.extract_all(data)
        return products, False

    async def _crawl_page_with_retry(self, session, page_num: int) -> list[dict]:
        for attempt in range(MAX_PAGE_RETRIES + 1):
            products, blocked = await self._crawl_single_page(session, page_num)
            if products:
                return products
            if blocked:
                if attempt < MAX_PAGE_RETRIES:
                    wait = BLOCK_BACKOFF[attempt]
                    logger.warning(f"Page {page_num} blocked. Waiting {wait}s before retry {attempt + 1}.")
                    await asyncio.sleep(wait)
            else:
                if attempt < MAX_PAGE_RETRIES:
                    logger.warning(f"Page {page_num} empty (non-block). Retrying in 10s.")
                    await asyncio.sleep(10)
        return []

    # ------------------------------------------------------------------
    # Pipeline
    # ------------------------------------------------------------------

    async def crawl_pipeline(self) -> dict:
        logger.info(f"Starting Woolies crawl pipeline (up to {self.max_pages} pages, single session)")

        all_products: list[dict] = []
        seen_keys: set[str] = set()
        pages_succeeded = 0
        pages_blocked = 0
        pages_attempted = 0
        consecutive_failures = 0

        async with self._new_session() as session:
            await self._warmup(session)

            for page_num in range(1, self.max_pages + 1):
                pages_attempted = page_num
                logger.info(f"Page {page_num}/{self.max_pages}")
                products = await self._crawl_page_with_retry(session, page_num)

                if products:
                    new_products = []
                    for p in products:
                        key = p.get('product_link') or p.get('name')
                        if key not in seen_keys:
                            seen_keys.add(key)
                            new_products.append(p)

                    if not new_products:
                        # Woolies re-serves earlier products past the last page;
                        # a page with zero NEW products means pagination ended.
                        logger.info(f"Page {page_num}: no new products — end of pagination")
                        break

                    all_products.extend(new_products)
                    pages_succeeded += 1
                    consecutive_failures = 0
                    logger.info(f"Page {page_num}: {len(new_products)} new products. Total: {len(all_products)}")
                else:
                    pages_blocked += 1
                    consecutive_failures += 1
                    logger.warning(f"Page {page_num}: 0 products after all retries")
                    if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                        logger.error(f"{consecutive_failures} consecutive failed pages — aborting crawl early")
                        break

                if page_num < self.max_pages:
                    await human_delay(3, 7)

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
            "crawler_version": "woolies-v2",
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
