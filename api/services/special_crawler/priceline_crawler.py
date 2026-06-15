import json
import boto3
import logging
import asyncio
from datetime import datetime, timezone
from scrapling.fetchers import AsyncStealthySession
from core.settings import get_settings
from services.special_crawler.discounts import classify_discount

PRICELINE_BASE_URL = "https://www.priceline.com.au"
PRICELINE_API_BASE = "https://api.priceline.com.au"
PRICELINE_SALE_URL = f"{PRICELINE_BASE_URL}/c/sale"
# Priceline runs on SAP Commerce (Hybris OCC). Its product data comes from the
# OCC REST API on api.priceline.com.au. Navigating to that API in the top frame
# serves HTML, and capturing the auto-fired XHR yields an empty body — so,
# unlike Coles/Woolies/CW, we call the API from *inside* the loaded /c/sale page
# via fetch() (page.evaluate). Same stealth session + frozen output, different
# fetch mechanism (the user sanctioned per-retailer implementations).
PRICELINE_OCC_PATH = "/occ/v2/priceline/products/search"
PRICELINE_FIELDS = (
    "products(code,name,url,price(FULL),discountedPrice(FULL),"
    "images(DEFAULT),promotionName,brandName),pagination(DEFAULT)"
)
PRICELINE_QUERY = ":relevance:allCategories:sale"
PAGE_SIZE = 36
DATA_NODE_ID = "__plp_data"

MIN_PRODUCTS_TO_SAVE = 40
MIN_PRODUCTS_SUCCESS = 150
MAX_PAGES = 40
# In-page JS pagination budget (ms). Kept under the page timeout so evaluate
# isn't killed mid-loop. Outer wall-time bound below is the hard ceiling.
JS_BUDGET_MS = 240000
MAX_CRAWL_SECONDS = 600

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


# JS run inside the /c/sale page: paginate the OCC search API and return all
# product JSON. Plain GET fetch (no custom headers → no CORS preflight).
_PAGINATE_JS = """
async (cfg) => {
    const start = Date.now();
    const all = [];
    let pagination = null;
    for (let pg = 0; pg < cfg.maxPages; pg++) {
        if (Date.now() - start > cfg.budgetMs) break;
        const url = cfg.apiBase + cfg.path
            + "?fields=" + encodeURIComponent(cfg.fields)
            + "&query=" + encodeURIComponent(cfg.query)
            + "&pageSize=" + cfg.pageSize
            + "&currentPage=" + pg
            + "&lang=en&curr=AUD";
        let r;
        try { r = await fetch(url); } catch (e) { continue; }
        if (!r.ok) continue;
        let d;
        try { d = await r.json(); } catch (e) { continue; }
        const prods = (d && d.products) || [];
        if (!prods.length) break;
        all.push(...prods);
        pagination = d.pagination || null;
        if (pagination && pg >= (pagination.totalPages - 1)) break;
        await new Promise(res => setTimeout(res, 250 + Math.random() * 400));
    }
    return JSON.stringify({ products: all, pagination: pagination });
}
"""


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
        logger.info("Initializing PricelineCrawler (scrapling 0.4 / in-page OCC API fetch)")
        self.max_pages = MAX_PAGES
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
            self.file_key = '/home/crawlers/priceline_specials.json'
            logger.info("S3 client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize S3 client: {e}")
            raise

    def _new_session(self) -> AsyncStealthySession:
        return AsyncStealthySession(
            headless=self.headless,
            block_webrtc=False,
            locale="en-AU",
            timezone_id="Australia/Sydney",
            google_search=True,
            timeout=300000,  # one navigation does the whole JS pagination loop
            wait=3000,
            retries=1,
        )

    async def _paginate_action(self, page):
        """page_action: from inside the loaded /c/sale page, fetch the OCC API
        page-by-page and stash the combined JSON in a DOM node we can read."""
        budget_ms = min(JS_BUDGET_MS, int(MAX_CRAWL_SECONDS * 1000 * 0.8))
        cfg = {
            "apiBase": PRICELINE_API_BASE,
            "path": PRICELINE_OCC_PATH,
            "fields": PRICELINE_FIELDS,
            "query": PRICELINE_QUERY,
            "pageSize": PAGE_SIZE,
            "maxPages": self.max_pages,
            "budgetMs": budget_ms,
        }
        result = await page.evaluate(_PAGINATE_JS, cfg)
        await page.evaluate(
            """(args) => { const d = document.createElement('div'); d.id = args.id;
                 d.textContent = args.t; document.body.appendChild(d); }""",
            {"id": DATA_NODE_ID, "t": result},
        )

    async def crawl_pipeline(self) -> dict:
        logger.info(f"Starting Priceline crawl pipeline (up to {self.max_pages} pages via in-page OCC fetch)")
        loop = asyncio.get_event_loop()
        deadline = loop.time() + MAX_CRAWL_SECONDS

        raw_products: list[dict] = []
        pagination = {}
        async with self._new_session() as session:
            try:
                response = await session.fetch(PRICELINE_SALE_URL, page_action=self._paginate_action)
            except Exception as exc:
                logger.error(f"Fetch error: {exc}")
                response = None

            if response is not None and loop.time() < deadline + 1:
                node = response.css(f'#{DATA_NODE_ID}')
                if node:
                    try:
                        payload = json.loads(node.first.get_all_text())
                        raw_products = payload.get("products", [])
                        pagination = payload.get("pagination") or {}
                    except Exception as exc:
                        logger.error(f"Could not parse in-page payload: {exc}")
                else:
                    logger.warning("No data node found — page may have been blocked")

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
        pages_done = len(raw_products) // PAGE_SIZE if raw_products else 0

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
            "pages_attempted": pages_done,
            "pages_succeeded": pages_done,
            "pages_blocked": 0 if raw_products else 1,
            "crawler_version": "priceline-v1",
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
        return self.load_from_file()
