import json
import boto3
import logging
import asyncio
import random
from datetime import datetime, timezone
from scrapling.fetchers import AsyncStealthySession
from core.settings import get_settings
from services.special_crawler.discounts import classify_discount

CW_BASE_URL = "https://www.chemistwarehouse.com.au"
# Chemist Warehouse is a Next.js app behind Cloudflare; its category pages load
# products from Algolia. We run the same stealth browser session as the other
# crawlers and capture the Algolia response (clean structured JSON), mirroring
# the Woolworths approach. "Clearance" is CW's genuine markdown category — CW
# shows an RRP comparison on almost everything, so a general category would be
# nearly the whole catalogue; clearance is the honest "specials" analogue.
CW_CATEGORY_URL = f"{CW_BASE_URL}/shop-online/3240/clearance"
CW_XHR_PATTERN = "algolia.net"
TILE_SELECTOR = "a[href*='/buy/']"

BLOCK_SIGNALS = [
    "Attention Required",          # Cloudflare block page
    "Sorry, you have been blocked",
    "cf-error-details",
    "challenge-platform",
]

MIN_PRODUCTS_TO_SAVE = 40
MIN_PRODUCTS_SUCCESS = 150
MAX_PAGE_RETRIES = 2
BLOCK_BACKOFF = [20, 45]
MAX_CONSECUTIVE_FAILURES = 3
# Hard ceiling on total crawl wall-time (machine-sleep safety, see Coles/Woolies).
MAX_CRAWL_SECONDS = 600
PAGE_TIMEOUT_MS = 60000  # >=60s: solve_cloudflare needs the headroom

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_blocked(html: str) -> bool:
    return any(signal in html for signal in BLOCK_SIGNALS)


async def human_delay(min_s: float = 2.0, max_s: float = 4.0):
    await asyncio.sleep(random.uniform(min_s, max_s))


def _cents(value) -> float:
    try:
        return round(float(value) / 100.0, 2)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------

class ProductExtractor:
    """Builds the frozen product shape from a captured Algolia search response."""

    def extract_all(self, algolia_result: dict) -> list[dict]:
        hits = algolia_result.get("hits", []) if algolia_result else []
        products = []
        for h in hits:
            try:
                name = (h.get("name") or {}).get("en") or ""
                if not name:
                    continue
                price = _cents(h.get("calculatedPrice"))
                was_price = self._rrp(h)
                # Only keep products with a genuine was>now discount.
                if not (was_price > price > 0):
                    continue
                products.append({
                    "name": name,
                    "price": price,
                    "price_per_unit": "",
                    "price_was": was_price,
                    "product_link": self._link(h),
                    "image": self._image(h),
                    "discount": f"Save ${was_price - price:.2f}",
                    "discount_type": classify_discount(price, was_price),
                    "retailer": "Chemist Warehouse",
                })
            except Exception as exc:
                logger.debug(f"Hit extraction error: {exc}")
        logger.info(f"Extracted {len(products)} discounted products from page")
        return products

    def _rrp(self, hit) -> float:
        try:
            return _cents(hit["pricesCustomFields"]["AUD"]["rrp"]["centAmount"])
        except (KeyError, TypeError):
            return 0.0

    def _link(self, hit) -> str:
        slug = (hit.get("slug") or {}).get("en") or ""
        if not slug:
            return ""
        # slug is "<id>-<name-slug>"; the buy URL is /buy/<id>/<name-slug>
        head, _, tail = slug.partition("-")
        if head.isdigit() and tail:
            return f"{CW_BASE_URL}/buy/{head}/{tail}"
        return f"{CW_BASE_URL}/buy/{slug}"

    def _image(self, hit) -> str:
        images = hit.get("images") or []
        if images and isinstance(images[0], str):
            return images[0]
        return ""


# ---------------------------------------------------------------------------
# Production crawler
# ---------------------------------------------------------------------------

class ChemistWarehouseCrawler:
    def __init__(self):
        logger.info("Initializing ChemistWarehouseCrawler (scrapling 0.4 / Algolia XHR capture)")
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
            self.file_key = '/home/crawlers/chemist_warehouse_specials.json'
            logger.info("S3 client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize S3 client: {e}")
            raise

    # ------------------------------------------------------------------
    # Session helpers
    # ------------------------------------------------------------------

    def _new_session(self) -> AsyncStealthySession:
        """One persistent browser for the whole crawl. capture_xhr + solve_cloudflare
        must be set here (not per-fetch). CW sits behind Cloudflare."""
        return AsyncStealthySession(
            headless=self.headless,
            block_webrtc=False,
            locale="en-AU",
            timezone_id="Australia/Sydney",
            google_search=True,
            timeout=PAGE_TIMEOUT_MS,
            wait=2500,
            solve_cloudflare=True,
            capture_xhr=CW_XHR_PATTERN,
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
        logger.info(f"Warmup: {CW_BASE_URL}")
        try:
            response = await session.fetch(CW_BASE_URL)
            if response:
                logger.info(f"Warmup OK (status={response.status})")
        except Exception as exc:
            logger.warning(f"Warmup failed (non-fatal): {exc}")
        await human_delay(2, 5)

    # ------------------------------------------------------------------
    # Page crawl with retry
    # ------------------------------------------------------------------

    @staticmethod
    def _algolia_result(response) -> dict | None:
        """Return the products result block from the captured Algolia XHR(s)."""
        xhrs = getattr(response, "captured_xhr", None) or []
        for x in reversed(xhrs):
            try:
                data = x.json()
            except Exception:
                continue
            for res in (data.get("results") or []):
                if res.get("hits"):
                    return res
        return None

    async def _crawl_single_page(self, session, page_num: int) -> tuple[list[dict], bool]:
        url = CW_CATEGORY_URL if page_num == 1 else f"{CW_CATEGORY_URL}?page={page_num}"
        response = await self._fetch(session, url)
        if not response:
            return [], False

        if is_blocked(response.html_content):
            logger.warning(f"Page {page_num}: blocked (Cloudflare)")
            return [], True

        result = self._algolia_result(response)
        if result is None:
            logger.warning(f"Page {page_num}: no Algolia payload captured")
            return [], False

        products = self.extractor.extract_all(result)
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
        logger.info(f"Starting Chemist Warehouse crawl pipeline (up to {self.max_pages} pages)")

        all_products: list[dict] = []
        seen_keys: set[str] = set()
        pages_succeeded = 0
        pages_blocked = 0
        pages_attempted = 0
        consecutive_failures = 0
        loop = asyncio.get_event_loop()
        deadline = loop.time() + MAX_CRAWL_SECONDS

        async with self._new_session() as session:
            await self._warmup(session)

            for page_num in range(1, self.max_pages + 1):
                if loop.time() >= deadline:
                    logger.warning(
                        f"Crawl wall-time budget ({MAX_CRAWL_SECONDS}s) exceeded — "
                        f"stopping at page {page_num - 1} with {len(all_products)} products"
                    )
                    break
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
            "crawler_version": "cw-v1",
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
            response = self.s3_client.get_object(Bucket=self.bucket_name, Key=self.file_key)
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
    # Public interface (matches the Coles/Woolies contract)
    # ------------------------------------------------------------------

    async def force_sync(self) -> dict | None:
        logger.info("Starting force_sync (Chemist Warehouse)")
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
