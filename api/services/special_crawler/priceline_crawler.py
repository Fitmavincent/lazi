import json
import boto3
import logging
import asyncio
from datetime import datetime, timezone
import requests
from core.settings import get_settings
from services.special_crawler.discounts import classify_discount

PRICELINE_BASE_URL = "https://www.priceline.com.au"
PRICELINE_API_BASE = "https://api.priceline.com.au"
PRICELINE_OCC_PATH = "/occ/v2/priceline/products/search"
PRICELINE_FIELDS = (
    "products(code,name,url,price(FULL),discountedPrice(FULL),"
    "images(DEFAULT),promotionName,brandName),pagination(DEFAULT)"
)
PRICELINE_QUERY = ":relevance:allCategories:sale"
PAGE_SIZE = 36
MIN_PRODUCTS_TO_SAVE = 40
MIN_PRODUCTS_SUCCESS = 150
MAX_PAGES = 40
MAX_CRAWL_SECONDS = 240
REQUEST_TIMEOUT = (5, 15)
PAGE_DELAY_SECONDS = 0.4

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------

class ProductExtractor:
    """Builds the frozen product shape from OCC search product objects."""

    def extract_all(self, products_raw: list[dict]) -> list[dict]:
        products = []
        for p in products_raw or []:
            try:
                name = p.get("name") or ""
                if not name:
                    continue
                now = self._val(p.get("discountedPrice"))
                was = self._val(p.get("price"))
                # Only keep products with a genuine was>now discount.
                if not (was > now > 0):
                    continue
                products.append({
                    "name": name,
                    "price": now,
                    "price_per_unit": "",
                    "price_was": was,
                    "product_link": self._link(p),
                    "image": self._image(p),
                    "discount": f"Save ${was - now:.2f}",
                    "discount_type": classify_discount(now, was),
                    "retailer": "Priceline",
                })
            except Exception as exc:
                logger.debug(f"Product extraction error: {exc}")
        logger.info(f"Extracted {len(products)} discounted products")
        return products

    def _val(self, price_obj) -> float:
        try:
            return float(price_obj.get("value"))
        except (AttributeError, TypeError, ValueError):
            return 0.0

    def _link(self, p) -> str:
        url = p.get("url") or ""
        if not url:
            return ""
        return url if url.startswith("http") else f"{PRICELINE_BASE_URL}{url}"

    def _image(self, p) -> str:
        images = p.get("images") or []
        # prefer the larger "product" format PRIMARY image, else first PRIMARY/any
        best = None
        for img in images:
            if img.get("imageType") == "PRIMARY":
                if img.get("format") == "product":
                    best = img
                    break
                best = best or img
        best = best or (images[0] if images else None)
        if not best:
            return ""
        url = best.get("url") or ""
        if not url:
            return ""
        return url if url.startswith("http") else f"{PRICELINE_API_BASE}{url}"


# ---------------------------------------------------------------------------
# Production crawler
# ---------------------------------------------------------------------------

class PricelineCrawler:
    def __init__(self):
        logger.info("Initializing PricelineCrawler (direct OCC API)")
        self.max_pages = MAX_PAGES
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
            self.file_key = '/home/crawlers/priceline_specials.json'
            logger.info("S3 client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize S3 client: {e}")
            raise

    def _fetch_page(self, session: requests.Session, page: int) -> dict:
        response = session.get(
            PRICELINE_API_BASE + PRICELINE_OCC_PATH,
            params={
                "fields": PRICELINE_FIELDS,
                "query": PRICELINE_QUERY,
                "pageSize": PAGE_SIZE,
                "currentPage": page,
                "lang": "en",
                "curr": "AUD",
            },
            timeout=REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("products"), list):
            raise ValueError("OCC response is missing its products list")
        return payload

    async def crawl_pipeline(self) -> dict:
        logger.info("Starting Priceline crawl pipeline (up to %s pages via OCC API)", self.max_pages)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + MAX_CRAWL_SECONDS
        raw_products: list[dict] = []
        pages_attempted = pages_succeeded = pages_blocked = 0

        with requests.Session() as session:
            for page in range(self.max_pages):
                if loop.time() >= deadline:
                    break
                pages_attempted += 1
                try:
                    payload = await asyncio.to_thread(self._fetch_page, session, page)
                except requests.HTTPError as exc:
                    status = exc.response.status_code if exc.response is not None else None
                    pages_blocked = int(status in (401, 403, 429))
                    logger.warning("Priceline OCC page %s returned HTTP %s", page, status)
                    break
                except (requests.RequestException, ValueError) as exc:
                    logger.warning("Could not read Priceline OCC page %s: %s", page, exc)
                    break

                pages_succeeded += 1
                products = payload["products"]
                raw_products.extend(p for p in products if isinstance(p, dict))
                pagination = payload.get("pagination")
                total_pages = pagination.get("totalPages") if isinstance(pagination, dict) else None
                if not products or (isinstance(total_pages, int) and page + 1 >= total_pages):
                    break
                if page + 1 < self.max_pages:
                    await asyncio.sleep(PAGE_DELAY_SECONDS)

        # Dedupe by product link / code
        seen: set[str] = set()
        deduped = []
        for p in raw_products:
            key = (p.get("code") or p.get("url") or json.dumps(p))
            if key not in seen:
                seen.add(key)
                deduped.append(p)

        all_products = self.extractor.extract_all(deduped)
        n = len(all_products)

        if n >= MIN_PRODUCTS_SUCCESS and pages_succeeded == pages_attempted:
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
            "crawler_version": "priceline-v2",
            "count": n,
            "data": all_products,
        }

    # ------------------------------------------------------------------
    # Storage
    # ------------------------------------------------------------------

    def save_to_file(self, data: dict):
        logger.info("Saving data to Cloudflare R2")
        try:
            self.s3_client.put_object(Bucket=self.bucket_name, Key=self.file_key, Body=json.dumps(data))
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
    # Public interface
    # ------------------------------------------------------------------

    async def force_sync(self) -> dict | None:
        logger.info("Starting force_sync (Priceline)")
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
        return await asyncio.to_thread(self.load_from_file)
