import json
import boto3
import logging
import asyncio
import random
from datetime import datetime, timezone
from scrapling.fetchers import StealthyFetcher
from urllib.parse import urljoin
from core.settings import get_settings

COLES_BASE_URL = "https://www.coles.com.au"
COLES_SPECIAL_URL = f"{COLES_BASE_URL}/on-special?filter_Special=halfprice"

WARMUP_URLS = [
    "https://www.coles.com.au",
    "https://www.coles.com.au/specials",
]

BLOCK_SIGNALS = [
    "Pardon Our Interruption",
    "interstitial-inprogress",
    # NOTE: "/_Incapsula_Resource" is embedded in ALL Coles pages (their CDN script)
    # — do NOT use it as a block signal; it causes false positives on valid 200 responses.
    "challenge-platform",
    "cf-challenge",
]

CONTAINER_SELECTORS = [
    'div[data-testid="specials-product-tiles"]',
    '[data-testid="product-grid"]',
    'main ul[class*="product"]',
]

NAME_SELECTORS = [
    'a.product__link.product__image[aria-label]',
    '[data-testid="product-tile"] h2',
    '[data-testid="product-tile"] .product__title',
]

PRICE_SELECTORS = [
    '[data-testid="product-pricing"][aria-label]',
    '.price__value',
    '[class*="price"][class*="current"]',
]

MIN_PRODUCTS_TO_SAVE = 50
MIN_PRODUCTS_SUCCESS = 200
MAX_PAGE_RETRIES = 2
BLOCK_BACKOFF = [30, 90]

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def is_blocked(html: str) -> bool:
    return any(signal in html for signal in BLOCK_SIGNALS)


def is_empty_render(html: str) -> bool:
    return len(html) < 5000 and "product" not in html.lower()


async def human_delay(min_s: float = 4.0, max_s: float = 9.0):
    await asyncio.sleep(random.uniform(min_s, max_s))


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------

class ProductExtractor:
    def find_container(self, response):
        for sel in CONTAINER_SELECTORS:
            container = response.css_first(sel)
            if container:
                logger.info(f"Container found with selector: {sel!r}")
                return container
        logger.warning("No product container found — possible selector failure; Coles may have updated markup")
        return None

    def extract_name(self, element) -> str | None:
        link = element.css_first('a.product__link.product__image')
        if link:
            label = link.attrib.get('aria-label', '')
            if label:
                return label.split(' | ')[0].strip()
        for sel in NAME_SELECTORS[1:]:
            el = element.css_first(sel)
            if el and el.text:
                return el.text.strip()
        return None

    def extract_price(self, element) -> float:
        price_el = element.css_first('[data-testid="product-pricing"]')
        if price_el:
            label = price_el.attrib.get('aria-label', '')
            if 'Price $' in label:
                try:
                    return float(label.replace('Price $', '').strip())
                except ValueError:
                    pass
        for sel in PRICE_SELECTORS[1:]:
            el = element.css_first(sel)
            if el and el.text:
                text = el.text.strip().lstrip('$').replace(',', '')
                try:
                    return float(text)
                except ValueError:
                    pass
        return 0.0

    def extract_was_and_unit(self, element) -> tuple[float, str]:
        was_price = 0.0
        unit_price = ''
        calc_el = element.css_first('.price__calculation_method')
        if calc_el:
            calc_text = calc_el.text or ''
            if ' | Was $' in calc_text:
                parts = calc_text.split(' | Was $')
                unit_price = parts[0].strip()
                try:
                    was_price = float(parts[1].strip())
                except (ValueError, IndexError):
                    pass
            else:
                unit_price = calc_text.strip()
        return was_price, unit_price

    def extract_image(self, element) -> str:
        img = element.css_first('[data-testid="product-image"]')
        if not img:
            return ''
        srcset = img.attrib.get('srcset', '')
        if srcset:
            first = srcset.split(' ')[0]
            return urljoin(COLES_BASE_URL, first) if first.startswith('/') else first
        src = img.attrib.get('src', '')
        return urljoin(COLES_BASE_URL, src) if src.startswith('/') else src

    def extract_discount(self, element, was_price: float, current_price: float) -> str:
        badge = element.css_first('.badge-label')
        if badge and badge.text and 'Save' in badge.text:
            return badge.text.strip()
        if was_price > current_price > 0:
            return 'Half Price'
        return ''

    def extract_link(self, element) -> str:
        link = element.css_first('a.product__link.product__image')
        if link:
            href = link.attrib.get('href', '')
            return urljoin(COLES_BASE_URL, href) if href.startswith('/') else href
        return ''

    def extract_all(self, response) -> list[dict]:
        container = self.find_container(response)
        if not container:
            return []
        tiles = container.css('section[data-testid="product-tile"]')
        if not tiles:
            logger.warning("No product tiles found inside container")
            return []
        logger.info(f"Found {len(tiles)} product tiles")
        products = []
        for i, tile in enumerate(tiles):
            try:
                name = self.extract_name(tile)
                if not name:
                    continue
                price = self.extract_price(tile)
                was_price, unit_price = self.extract_was_and_unit(tile)
                products.append({
                    'name': name,
                    'price': price,
                    'price_per_unit': unit_price,
                    'price_was': was_price,
                    'product_link': self.extract_link(tile),
                    'image': self.extract_image(tile),
                    'discount': self.extract_discount(tile, was_price, price),
                    'retailer': 'Coles',
                })
            except Exception as exc:
                logger.debug(f"Tile {i+1} extraction error: {exc}")
        logger.info(f"Extracted {len(products)} products from page")
        return products


# ---------------------------------------------------------------------------
# Production crawler
# ---------------------------------------------------------------------------

class ColesV25Crawler:
    def __init__(self):
        logger.info("Initializing ColesV25Crawler")
        self.max_pages = 20
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
            self.file_key = '/home/crawlers/coles_specials_v2_5.json'
            logger.info("S3 client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize S3 client: {e}")
            raise

    # ------------------------------------------------------------------
    # Fetch helpers
    # ------------------------------------------------------------------

    async def _fetch(self, url: str) -> object | None:
        logger.info(f"Fetching: {url}")
        try:
            response = await StealthyFetcher.async_fetch(
                url,
                headless=True,
                timeout=60000,
                wait=5000,
                humanize=True,
                block_webrtc=False,
                geoip=True,
                disable_ads=False,
                google_search=True,
            )
            return response
        except Exception as exc:
            logger.error(f"Fetch error for {url}: {exc}")
            return None

    async def _warmup(self):
        for url in WARMUP_URLS:
            logger.info(f"Warmup: {url}")
            response = await self._fetch(url)
            if response:
                logger.info(f"Warmup OK: {url} (status={response.status})")
            await human_delay(3, 6)

    # ------------------------------------------------------------------
    # Page crawl with retry
    # ------------------------------------------------------------------

    async def _crawl_single_page(self, page_num: int) -> tuple[list[dict], bool]:
        url = COLES_SPECIAL_URL if page_num == 1 else f"{COLES_SPECIAL_URL}&page={page_num}"
        response = await self._fetch(url)
        if not response:
            return [], False

        html = str(response)

        if is_blocked(html):
            logger.warning(f"Page {page_num}: blocked by anti-bot protection")
            return [], True

        if is_empty_render(html):
            logger.warning(f"Page {page_num}: empty render (len={len(html)})")
            return [], False

        products = self.extractor.extract_all(response)
        return products, False

    async def _crawl_page_with_retry(self, page_num: int) -> list[dict]:
        for attempt in range(MAX_PAGE_RETRIES + 1):
            products, blocked = await self._crawl_single_page(page_num)
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
        logger.info(f"Starting V2.5 crawl pipeline ({self.max_pages} pages)")
        await self._warmup()

        all_products: list[dict] = []
        pages_succeeded = 0
        pages_blocked = 0

        for page_num in range(1, self.max_pages + 1):
            logger.info(f"Page {page_num}/{self.max_pages}")
            products = await self._crawl_page_with_retry(page_num)

            if products:
                all_products.extend(products)
                pages_succeeded += 1
                logger.info(f"Page {page_num}: {len(products)} products. Total: {len(all_products)}")
            else:
                pages_blocked += 1
                logger.warning(f"Page {page_num}: 0 products after all retries")

            if page_num < self.max_pages:
                await human_delay(4, 9)

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
            "pages_attempted": self.max_pages,
            "pages_succeeded": pages_succeeded,
            "pages_blocked": pages_blocked,
            "crawler_version": "v2.5",
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
    # Public interface (matches V2 contract)
    # ------------------------------------------------------------------

    async def force_sync(self) -> dict | None:
        logger.info("Starting force_sync (V2.5)")
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
