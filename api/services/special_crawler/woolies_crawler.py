import json
import boto3
from datetime import datetime
from playwright.async_api import async_playwright, Route, Request
from fake_useragent import UserAgent
from core.settings import get_settings

WOOLIES_BASE_URL = "https://www.woolworths.com.au"
WOOLIES_SPECIAL_URL = f"{WOOLIES_BASE_URL}/shop/browse/specials/half-price"
API_URL_PATTERN = "**/apis/ui/browse/category*"

ua = UserAgent(browsers=['firefox', 'chrome', 'safari', 'Edge'])

class WooliesCrawler:
    def __init__(self):
        settings = get_settings()
        self.s3_client = boto3.client(
            service_name='s3',
            endpoint_url=settings.R2_ENDPOINT_URL,
            aws_access_key_id=settings.R2_ACCESS_KEY_ID,
            aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
            region_name=settings.R2_REGION
        )
        self.bucket_name = settings.R2_BUCKET_NAME
        self.file_key = '/home/crawlers/woolies_specials.json'
        self.all_products = []
        self.current_page = 1
        self.max_pages = 10
        self.page_stats = []  # Add tracking for page statistics

    async def handle_request(self, route: Route, request: Request):
        print(f"Intercepted request to: {route.request.url}")

        # Let the first request go through normally
        if self.current_page == 1:
            response = await route.fetch()
            json_data = await response.json()
            self.process_response(json_data)
            self.page_stats.append({
                'page': self.current_page,
                'products_found': len(json_data.get('Bundles', [])),
                'total_records': json_data.get('TotalRecordCount', 0)
            })
            self.current_page += 1
            await self.fetch_remaining_pages(request)
            await route.continue_()
        else:
            await route.continue_()

    async def fetch_remaining_pages(self, original_request):
        base_payload = {
            "categoryId": "specialsgroup.3676",
            "pageSize": 36,
            "sortType": "TraderRelevance",
            "url": "/shop/browse/specials/half-price",
            "location": "/shop/browse/specials/half-price",
            "formatObject": "{\"name\":\"Half Price\"}",
            "isSpecial": True,
            "isBundle": False,
            "isMobile": False,
            "filters": [],
            "token": "",
            "gpBoost": 0,
            "isHideUnavailableProducts": False,
            "isRegisteredRewardCardPromotion": False,
            "enableAdReRanking": False,
            "groupEdmVariants": True,
            "categoryVersion": "v2",
            "flags": {"EnableProductBoostExperiment": True}
        }

        # Start from page 2 since we already have page 1
        for page in range(2, self.max_pages + 1):
            print(f"Fetching page {page}")
            payload = base_payload.copy()
            payload["pageNumber"] = page

            try:
                async with async_playwright() as p:
                    browser = await p.firefox.launch(headless=True)
                    context = await browser.new_context(user_agent=ua.random)
                    page_instance = await context.new_page()

                    response = await page_instance.request.post(
                        f"{WOOLIES_BASE_URL}/apis/ui/browse/category",
                        headers={
                            'Content-Type': 'application/json',
                            'Origin': WOOLIES_BASE_URL,
                            'Referer': WOOLIES_SPECIAL_URL
                        },
                        data=json.dumps(payload)
                    )

                    if response.ok:
                        json_data = await response.json()
                        self.process_response(json_data)
                        self.page_stats.append({
                            'page': page,
                            'products_found': len(json_data.get('Bundles', [])),
                            'total_records': json_data.get('TotalRecordCount', 0)
                        })
                        self.current_page = page

                    await browser.close()
            except Exception as e:
                print(f"Error fetching page {page}: {e}")
                break

    def process_response(self, json_data):
        if not json_data.get('Success'):
            return

        for bundle in json_data.get('Bundles', []):
            for product in bundle.get('Products', []):
                if product.get('IsHalfPrice'):  # Only include half price items
                    self.all_products.append({
                        'name': product.get('DisplayName', ''),
                        'price_now': product.get('Price', 0),
                        'price_was': product.get('WasPrice', 0),
                        'price_per_unit': product.get('CupString', ''),
                        'image': product.get('LargeImageFile', ''),
                        'product_link': f"{WOOLIES_BASE_URL}/shop/productdetails/{product.get('Stockcode')}",
                    })

    def transform_product_data(self, raw_data):
        if not raw_data:
            return None

        transformed_data = []
        for product in raw_data:
            transformed_item = {
                'name': product.get('name', ''),
                'price': product.get('price_now', 0),
                'price_per_unit': product.get('price_per_unit', ''),
                'price_was': product.get('price_was', 0),
                'product_link': product.get('product_link', ''),
                'image': product.get('image', ''),
                'discount': '50% off',
                'retailer': 'Woolworths'
            }
            transformed_data.append(transformed_item)

        woolies_data = {
            'synced_at': datetime.now().isoformat(),
            'count': len(transformed_data),
            'data': transformed_data
        }
        return woolies_data

    async def crawl_woolies_pipeline(self):
        self.all_products = []  # Reset products list
        self.current_page = 1   # Reset page counter
        self.page_stats = []    # Reset page statistics
        original_max_pages = self.max_pages  # Store original max_pages

        async with async_playwright() as p:
            browser = await p.firefox.launch(headless=True)
            context = await browser.new_context(user_agent=ua.random)
            page = await context.new_page()

            try:
                # Intercept the API request
                await page.route(API_URL_PATTERN, self.handle_request)

                # Navigate to trigger the first request
                await page.goto(WOOLIES_SPECIAL_URL, timeout=30000)

                # Wait for all requests to complete
                await page.wait_for_timeout(5000)

            except Exception as e:
                print(f"Failed to load page or intercept API: {e}")
                return None
            finally:
                await browser.close()
                self.max_pages = original_max_pages  # Restore original max_pages

        return {
            'products': self.all_products,
            'pagination': self.page_stats
        }

    def save_to_file(self, data):
        """Save data to Cloudflare R2"""
        try:
            json_data = json.dumps(data)
            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=self.file_key,
                Body=json_data
            )
        except Exception as e:
            print(f"Error saving to R2: {e}")
            raise

    def load_from_file(self):
        """Load data from Cloudflare R2"""
        try:
            response = self.s3_client.get_object(
                Bucket=self.bucket_name,
                Key=self.file_key
            )
            json_data = response['Body'].read().decode('utf-8')
            return json.loads(json_data)
        except self.s3_client.exceptions.NoSuchKey:
            return None
        except Exception as e:
            print(f"Error loading from R2: {e}")
            return None

    async def force_sync(self):
        """Force sync data from Woolworths and save to file"""
        raw_data = await self.crawl_woolies_pipeline()
        if raw_data:
            transformed_data = self.transform_product_data(raw_data)
            self.save_to_file(transformed_data)
            return transformed_data
        return None

    async def fetch_data(self):
        """Only read from saved file"""
        return self.load_from_file()
