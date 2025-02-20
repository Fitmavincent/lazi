import json
import os
import boto3
from datetime import datetime, timezone
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Route, Request
import random
import asyncio
from fake_useragent import UserAgent
from core.settings import get_settings

COLES_BASE_URL = "https://www.coles.com.au"
COLES_CDN_URL = "https://shop.coles.com.au"
API_URL_PATTERN = "**/api/product*"

USER_AGENTS_LIST = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.2227.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.3497.92 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36',
]

ua = UserAgent(browsers=['firefox', 'chrome', 'safari', 'Edge'])

class ColesCrawler:
    def __init__(self):
        self.special_api_response = None
        settings = get_settings()

        # Initialize S3 client for Cloudflare R2
        self.s3_client = boto3.client(
            service_name='s3',
            endpoint_url=settings.R2_ENDPOINT_URL,
            aws_access_key_id=settings.R2_ACCESS_KEY_ID,
            aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
            region_name=settings.R2_REGION
        )
        self.bucket_name = settings.R2_BUCKET_NAME
        self.file_key = '/home/crawlers/coles_specials.json'

    async def handle_request(self, route: Route, request: Request):
        print(f"Requesting: {route.request.url}")
        response = await route.fetch()

        #debug log
        print(f"Response status: {response.status}")
        content = await response.body()
        print(f"Response content length: {len(content)} bytes")
        print(f"Response content: {content}")
        json_response = await response.json()
        truncated_response = str(json_response)[:500] + "..." if len(str(json_response)) > 500 else str(json_response)
        print(f"Response: {truncated_response}")
        #erroring here

        self.special_api_response = await response.json()

        await route.continue_()

    async def crawl_coles_pipeline(self):
        async with async_playwright() as p:
            browser = await p.firefox.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1600, "height": 1200},
                user_agent=ua.random
            )

            page = await context.new_page()

            await page.set_extra_http_headers({
                'Accept': '*/*',
                'Accept-Language': 'en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7,zh-TW;q=0.6',
                'Connection': 'keep-alive',
                'Content-Type': 'application/json',
                'origin':  f"{COLES_BASE_URL}",
                'referer': f"{COLES_BASE_URL}/half-price-specials"
            })

            try:
                # Intercept the API request
                await page.route(API_URL_PATTERN, self.handle_request)

                # Navigate to the specials page
                await page.goto(f"{COLES_BASE_URL}/half-price-specials", timeout=30000)
            except Exception as e:
                print(f"Failed to load page or intercept API: {e}")
                return None
            finally:
                await browser.close()

            return self.special_api_response

    def transform_product_data(self, raw_data):

        transformed_data = []
        for product in raw_data.get('results', []):
            transformed_item = {
                'name': product.get('description', ''),
                'price': product.get('pricing', {}).get('now', 0),
                'price_per_unit': product.get('pricing', {}).get('comparable', ''),
                'price_was': product.get('pricing', {}).get('was', 0),
                'product_link': f"{COLES_BASE_URL}/product/{product.get('id', '')}",
                'image': f"{COLES_BASE_URL}/_next/image?url=https://productimages.coles.com.au/productimages{product.get('imageUris', [{}])[0].get('uri', '')}&w=256&q=90" if product.get('imageUris') else '',
                "discount": product.get('pricing', {}).get('priceDescription', ''),
                "retailer": "Coles"
            }
            transformed_data.append(transformed_item)

        coles_data = {
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "count": raw_data.get('noOfResults', 0),
            "data": transformed_data
        }
        return coles_data

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
        """Force sync data from Coles API and save to file"""
        raw_data = await self.crawl_coles_pipeline()
        if (raw_data):
            transformed_data = self.transform_product_data(raw_data)
            self.save_to_file(transformed_data)
            return transformed_data
        return None

    async def fetch_data(self):
        """Only read from saved file"""
        return self.load_from_file()
