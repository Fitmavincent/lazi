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
        self.page_stats = []
        self.unique_product_names = set()  # Add tracking for unique products

    async def handle_request(self, route: Route, request: Request):
        print(f"Intercepted request to: {route.request.url} {datetime.now()}")
        print(f"Processing page {self.current_page}")

        response = await route.fetch()
        json_data = await response.json()

        self.process_response(json_data)
        self.page_stats.append({
            'page': self.current_page,
            'products_found': len(json_data.get('Bundles', [])),
            'total_records': json_data.get('TotalRecordCount', 0)
        })
        await route.continue_()

    async def crawl_woolies_pipeline(self):
        self.all_products = []  # Reset products list
        self.current_page = 1   # Reset page counter
        self.page_stats = []    # Reset page statistics
        self.unique_product_names = set()  # Reset unique products tracking

        async with async_playwright() as p:
            browser = await p.firefox.launch(headless=True)
            context = await browser.new_context(user_agent=ua.random)
            page = await context.new_page()

            try:
                # Set up request interception
                await page.route(API_URL_PATTERN, self.handle_request)

                # Navigate through pages
                for page_num in range(1, self.max_pages + 1):
                    self.current_page = page_num
                    url = WOOLIES_SPECIAL_URL
                    if page_num > 1:
                        url = f"{WOOLIES_SPECIAL_URL}?pageNumber={page_num}"

                    print(f"\nNavigating to page {page_num}: {url}")
                    await page.goto(url, timeout=30000)
                    await page.wait_for_timeout(3000)  # Wait for data to load

            except Exception as e:
                print(f"Failed to load page or intercept API: {e}")
                return None
            finally:
                print(f"Hit finally block {datetime.now()}")
                await browser.close()

        return {
            'products': self.all_products,
            'pagination': self.page_stats
        }

    def process_response(self, json_data):
        if not json_data.get('Success'):
            return

        for bundle in json_data.get('Bundles', []):
            for product in bundle.get('Products', []):
                if product.get('IsHalfPrice'):  # Only include half price items
                    product_name = product.get('DisplayName', '')
                    # Only add if we haven't seen this product name before
                    if product_name not in self.unique_product_names:
                        self.unique_product_names.add(product_name)
                        self.all_products.append({
                            'name': product_name,
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
        for product in raw_data.get('products', []):  # Access the 'products' key from raw_data
            transformed_item = {
                'name': product['name'],  # Direct dictionary access
                'price': product['price_now'],
                'price_per_unit': product['price_per_unit'],
                'price_was': product['price_was'],
                'product_link': product['product_link'],
                'image': product['image'],
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
        if (raw_data):
            transformed_data = self.transform_product_data(raw_data)
            self.save_to_file(transformed_data)
            return transformed_data
        return None

    async def fetch_data(self):
        """Only read from saved file"""
        return self.load_from_file()
