import json
import boto3
import logging
import asyncio
import os
from datetime import datetime, timezone
from playwright.async_api import async_playwright, Route, Request
from fake_useragent import UserAgent

COLES_BASE_URL = "https://www.coles.com.au"
COLES_CDN_URL = "https://shop.coles.com.au"
API_URL_PATTERN = "**/api/product*"

# Mock settings for local testing
class MockSettings:
    def __init__(self):
        # You'll need to replace these with your actual R2 credentials
        self.R2_ENDPOINT_URL = "https://your-account-id.r2.cloudflarestorage.com"
        self.R2_ACCESS_KEY_ID = "your-access-key"
        self.R2_SECRET_ACCESS_KEY = "your-secret-key"
        self.R2_REGION = "auto"
        self.R2_BUCKET_NAME = "your-bucket-name"

def get_settings():
    return MockSettings()

ua = UserAgent(browsers=['firefox', 'chrome', 'safari', 'Edge'])

# Set up logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class ColesCrawler:
    def __init__(self):
        logger.info("Initializing ColesCrawler")
        self.special_api_response = None
        settings = get_settings()

        # Initialize S3 client for Cloudflare R2
        try:
            self.s3_client = boto3.client(
                service_name='s3',
                endpoint_url=settings.R2_ENDPOINT_URL,
                aws_access_key_id=settings.R2_ACCESS_KEY_ID,
                aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
                region_name=settings.R2_REGION
            )
            self.bucket_name = settings.R2_BUCKET_NAME
            self.file_key = '/home/crawlers/coles_specials.json'
            logger.info("S3 client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize S3 client: {e}")
            # For local testing, we'll continue without S3
            self.s3_client = None

    async def handle_request(self, route: Route, request: Request):
        logger.info(f"Intercepting request to: {route.request.url}")
        try:
            response = await route.fetch()
            logger.info(f"Response status: {response.status}")

            if response.status != 200:
                logger.warning(f"Non-200 response status: {response.status}")
                await route.continue_()
                return

            content = await response.body()
            logger.info(f"Response content length: {len(content)} bytes")

            json_response = await response.json()
            logger.info(f"JSON response received with {len(json_response.get('results', []))} results")

            self.special_api_response = json_response
            await route.continue_()

        except Exception as e:
            logger.error(f"Error in handle_request: {e}")
            await route.continue_()

    async def crawl_coles_pipeline(self):
        logger.info("Starting Coles crawl pipeline")
        try:
            async with async_playwright() as p:
                logger.info("Launching browser")
                browser = await p.firefox.launch(headless=False)  # Set to False for debugging
                context = await browser.new_context(
                    viewport={"width": 1600, "height": 1200},
                    user_agent=ua.random
                )

                page = await context.new_page()
                logger.info("Browser page created")

                await page.set_extra_http_headers({
                    'Accept': '*/*',
                    'Accept-Language': 'en-US,en;q=0.9,zh-CN;q=0.8,zh;q=0.7,zh-TW;q=0.6',
                    'Connection': 'keep-alive',
                    'Content-Type': 'application/json',
                    'origin':  f"{COLES_BASE_URL}",
                    'referer': f"{COLES_BASE_URL}/on-special?filter_Special=halfprice"
                })

                try:
                    # Intercept the API request
                    logger.info("Setting up API request interception")
                    await page.route(API_URL_PATTERN, self.handle_request)

                    # Navigate to the specials page
                    logger.info(f"Navigating to {COLES_BASE_URL}/on-special?filter_Special=halfprice")
                    await page.goto(f"{COLES_BASE_URL}/on-special?filter_Special=halfprice", timeout=30000)

                    # Wait for the API call to complete
                    logger.info("Waiting for API response")
                    await page.wait_for_timeout(5000)

                    if self.special_api_response:
                        logger.info(f"API response received with {len(self.special_api_response.get('results', []))} products")
                    else:
                        logger.warning("No API response received")

                except Exception as e:
                    logger.error(f"Error during page navigation or API interception: {e}")
                    raise
                finally:
                    logger.info("Closing browser")
                    await browser.close()

            logger.info("Coles crawl pipeline completed successfully")
            return self.special_api_response

        except Exception as e:
            logger.error(f"Error in crawl_coles_pipeline: {e}")
            raise

    def transform_product_data(self, raw_data):
        logger.info("Starting data transformation")
        try:
            if not raw_data:
                logger.warning("No raw data to transform")
                return None

            transformed_data = []
            results = raw_data.get('results', [])
            logger.info(f"Transforming {len(results)} products")

            for product in results:
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

            logger.info(f"Data transformation completed. {len(transformed_data)} products transformed")
            return coles_data

        except Exception as e:
            logger.error(f"Error in transform_product_data: {e}")
            raise

    def save_to_file(self, data):
        """Save data to local file for debugging"""
        logger.info("Saving data to local file")
        try:
            # Save to local file instead of R2 for debugging
            filename = "coles_specials_debug.json"
            with open(filename, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            logger.info(f"Data successfully saved to local file: {filename}")

        except Exception as e:
            logger.error(f"Error saving to local file: {e}")
            raise

    async def force_sync(self):
        """Force sync data from Coles API and save to file"""
        logger.info("Starting force sync operation")
        try:
            raw_data = await self.crawl_coles_pipeline()
            if raw_data:
                logger.info("Raw data retrieved successfully, starting transformation")
                transformed_data = self.transform_product_data(raw_data)
                if transformed_data:
                    logger.info("Data transformation completed, saving to file")
                    self.save_to_file(transformed_data)
                    logger.info("Force sync operation completed successfully")
                    return transformed_data
                else:
                    logger.error("Data transformation failed")
                    return None
            else:
                logger.error("Failed to retrieve raw data")
                return None
        except Exception as e:
            logger.error(f"Error in force_sync: {e}")
            raise

async def main():
    """Main function for local debugging"""
    # Set up logging for console output
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    logger.info("Starting local debug for Coles crawler")

    try:
        # Initialize crawler
        crawler = ColesCrawler()

        # Test force_sync
        result = await crawler.force_sync()

        if result:
            logger.info(f"Force sync completed successfully!")
            logger.info(f"Products count: {result.get('count', 0)}")
            logger.info(f"Sample products: {result.get('data', [])[:3]}")
        else:
            logger.error("Force sync failed")

    except Exception as e:
        logger.error(f"Error in main: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
