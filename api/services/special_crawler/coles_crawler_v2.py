import json
import boto3
import logging
import asyncio
import re
from datetime import datetime, timezone
from scrapling.fetchers import StealthyFetcher
from urllib.parse import urljoin, urlparse
from core.settings import get_settings

COLES_BASE_URL = "https://www.coles.com.au"
COLES_SPECIAL_URL = f"{COLES_BASE_URL}/on-special?filter_Special=halfprice"

# Set up logging
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

class ColesV2Crawler:
    def __init__(self):
        logger.info("Initializing ColesV2Crawler with Scrapling StealthyFetcher")
        self.all_products = []
        self.max_pages = 20  # Crawl 15-20 pages for production

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
            self.file_key = '/home/crawlers/coles_specials_v2.json'
            logger.info("S3 client initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize S3 client: {e}")
            raise

    def extract_product_data(self, response):
        """Extract product data from the page HTML"""
        logger.info("Extracting product data from page")
        products = []

        try:
            # Get the soup from response
            soup = response

            # First, try to find the container with all product tiles
            container = soup.css_first('div[data-testid="specials-product-tiles"]')
            if not container:
                logger.warning("Could not find specials-product-tiles container")
                return []

            logger.info("Found specials-product-tiles container")

            # Look for individual product tiles within the container
            product_elements = container.css('section[data-testid="product-tile"]')

            if product_elements:
                logger.info(f"Found {len(product_elements)} product tiles")
            else:
                logger.warning("No product tiles found within container")
                return []

            for i, element in enumerate(product_elements):
                try:
                    product = self.extract_single_product(element)
                    if product and product.get('name'):  # Only add products with names
                        products.append(product)
                        logger.debug(f"Successfully extracted: {product.get('name')}")
                    else:
                        logger.debug(f"Product tile {i+1} failed extraction or has no name")
                except Exception as e:
                    logger.debug(f"Error extracting single product {i+1}: {e}")
                    continue

        except Exception as e:
            logger.error(f"Error in extract_product_data: {e}")

        logger.info(f"Successfully extracted {len(products)} products from page")
        return products

    def extract_single_product(self, element):
        """Extract data from a single product element"""
        product = {}

        try:
            # Product name - look for aria-label in the product link
            name_link = element.css_first('a.product__link.product__image')
            name = None
            if name_link:
                aria_label = name_link.attrib.get('aria-label', '')
                if aria_label:
                    # Clean up the name (remove size info after |)
                    name = aria_label.split(' | ')[0].strip()

            if not name:
                logger.debug("No product name found")
                return None

            product['name'] = name

            # Current price - look for data-testid="product-pricing"
            price_elem = element.css_first('[data-testid="product-pricing"]')
            current_price = 0
            if price_elem:
                aria_label = price_elem.attrib.get('aria-label', '')
                if aria_label and 'Price $' in aria_label:
                    price_text = aria_label.replace('Price $', '').strip()
                    try:
                        current_price = float(price_text)
                    except ValueError:
                        logger.debug(f"Could not parse current price: {price_text}")

            product['price'] = current_price

            # Was price and unit price - look in the calculation method area
            was_price = 0
            unit_price = ''

            calc_elem = element.css_first('.price__calculation_method')
            if calc_elem:
                calc_text = calc_elem.text  # Use .text property, not method

                # Extract unit price (everything before " | Was")
                if ' | Was $' in calc_text:
                    parts = calc_text.split(' | Was $')
                    unit_price = parts[0].strip()

                    # Extract was price
                    try:
                        was_price = float(parts[1].strip())
                    except (ValueError, IndexError):
                        logger.debug(f"Could not parse was price from: {calc_text}")
                else:
                    # If no "was" price, the whole text might be unit price
                    unit_price = calc_text

            product['price_was'] = was_price
            product['price_per_unit'] = unit_price

            # Product link
            link_elem = element.css_first('a.product__link.product__image')
            if link_elem:
                href = link_elem.attrib.get('href', '')
                if href.startswith('/'):
                    product_link = urljoin(COLES_BASE_URL, href)
                else:
                    product_link = href
            else:
                product_link = ''

            product['product_link'] = product_link

            # Image - look for data-testid="product-image"
            img_elem = element.css_first('[data-testid="product-image"]')
            image_url = ''
            if img_elem:
                # Try srcset first (for responsive images)
                srcset = img_elem.attrib.get('srcset', '')
                if srcset:
                    # Get the first URL from srcset
                    first_src = srcset.split(' ')[0]
                    if first_src.startswith('/_next/image'):
                        # This is a Next.js optimized image, need to construct full URL
                        image_url = urljoin(COLES_BASE_URL, first_src)
                    else:
                        image_url = first_src
                else:
                    # Fallback to src attribute
                    src = img_elem.attrib.get('src', '')
                    if src:
                        if src.startswith('/_next/image'):
                            image_url = urljoin(COLES_BASE_URL, src)
                        else:
                            image_url = src

            product['image'] = image_url

            # Discount info - look for savings badge
            discount = ''
            savings_elem = element.css_first('.badge-label')
            if savings_elem:
                savings_text = savings_elem.text  # Use .text property, not method
                if savings_text and 'Save' in savings_text:
                    discount = savings_text

            # If no specific discount text but has was_price, it's likely half price
            if not discount and was_price > current_price > 0:
                discount = "Half Price"

            product['discount'] = discount
            product['retailer'] = 'Coles'

            logger.debug(f"Extracted product: {name} - ${current_price}")
            return product

        except Exception as e:
            logger.debug(f"Error extracting single product: {e}")
            return None

    async def crawl_page(self, page_num=1):
        """Crawl a single page"""
        logger.info(f"Crawling page {page_num}")

        try:
            # Construct URL for the page
            if page_num == 1:
                url = COLES_SPECIAL_URL
            else:
                url = f"{COLES_SPECIAL_URL}&page={page_num}"

            logger.info(f"Fetching URL: {url}")

            # Use StealthyFetcher with optimized settings for production
            response = await StealthyFetcher.async_fetch(
                url,
                headless=True,  # Use headless for production
                timeout=45000,  # 45 seconds timeout
                wait=3000,  # Wait 3 seconds after page load
                humanize=True,  # Keep humanization
                block_webrtc=False,  # Don't block WebRTC
                geoip=True,  # Enable GeoIP
                disable_ads=False,  # Don't block ads to look more normal
                google_search=False,  # Don't use Google referer
            )

            if not response:
                logger.error(f"No response received for page {page_num}")
                return []

            logger.info(f"Page {page_num} fetched successfully, status: {response.status}")

            if response.status != 200:
                logger.warning(f"Non-200 status code for page {page_num}: {response.status}")
                return []

            # Check if we got blocked
            html_content = str(response)
            if "Pardon Our Interruption" in html_content or "interstitial-inprogress" in html_content:
                logger.error("Got blocked by anti-bot protection. The site detected automated access.")
                return []

            # Extract products from the page
            products = self.extract_product_data(response)

            logger.info(f"Page {page_num} crawled successfully, found {len(products)} products")
            return products

        except Exception as e:
            logger.error(f"Error crawling page {page_num}: {e}")
            return []

    async def crawl_coles_pipeline(self):
        """Crawl multiple pages from Coles website"""
        logger.info(f"Starting to crawl {self.max_pages} pages")
        self.all_products = []

        for page_num in range(1, self.max_pages + 1):
            try:
                logger.info(f"Processing page {page_num}/{self.max_pages}")
                products = await self.crawl_page(page_num)

                if not products:
                    logger.warning(f"No products found on page {page_num}, continuing...")
                    continue

                self.all_products.extend(products)
                logger.info(f"Added {len(products)} products from page {page_num}. Total: {len(self.all_products)}")

                # Add delay between pages to be respectful
                if page_num < self.max_pages:
                    logger.info("Waiting 3 seconds before next page...")
                    await asyncio.sleep(3)

            except Exception as e:
                logger.error(f"Error processing page {page_num}: {e}")
                continue

        logger.info(f"Crawling completed. Total products collected: {len(self.all_products)}")
        return self.all_products

    def transform_product_data(self, raw_products):
        """Transform raw product data to match the expected structure"""
        logger.info("Starting data transformation")

        try:
            if not raw_products:
                logger.warning("No raw products to transform")
                return None

            transformed_data = []
            logger.info(f"Transforming {len(raw_products)} products")

            for product in raw_products:
                # The product is already in the right format from extract_single_product
                transformed_item = {
                    'name': product.get('name', ''),
                    'price': product.get('price', 0),
                    'price_per_unit': product.get('price_per_unit', ''),
                    'price_was': product.get('price_was', 0),
                    'product_link': product.get('product_link', ''),
                    'image': product.get('image', ''),
                    'discount': product.get('discount', ''),
                    'retailer': product.get('retailer', 'Coles')
                }
                transformed_data.append(transformed_item)

            coles_data = {
                "synced_at": datetime.now(timezone.utc).isoformat(),
                "count": len(transformed_data),
                "data": transformed_data
            }

            logger.info(f"Data transformation completed. {len(transformed_data)} products transformed")
            return coles_data

        except Exception as e:
            logger.error(f"Error in transform_product_data: {e}")
            raise

    def save_to_file(self, data):
        """Save data to Cloudflare R2"""
        logger.info("Saving data to Cloudflare R2")
        try:
            json_data = json.dumps(data)
            logger.info(f"Serialized data size: {len(json_data)} bytes")

            self.s3_client.put_object(
                Bucket=self.bucket_name,
                Key=self.file_key,
                Body=json_data
            )
            logger.info(f"Data successfully saved to R2: {self.file_key}")

        except Exception as e:
            logger.error(f"Error saving to R2: {e}")
            raise

    def load_from_file(self):
        """Load data from Cloudflare R2"""
        logger.info("Loading data from Cloudflare R2")
        try:
            response = self.s3_client.get_object(
                Bucket=self.bucket_name,
                Key=self.file_key
            )
            json_data = response['Body'].read().decode('utf-8')
            data = json.loads(json_data)
            logger.info(f"Data successfully loaded from R2: {len(data.get('data', []))} products")
            return data

        except self.s3_client.exceptions.NoSuchKey:
            logger.warning("File not found in R2")
            return None
        except Exception as e:
            logger.error(f"Error loading from R2: {e}")
            return None

    async def force_sync(self):
        """Force sync data from Coles website and save to file"""
        logger.info("Starting force sync operation with Scrapling")

        try:
            raw_products = await self.crawl_coles_pipeline()

            if raw_products:
                logger.info("Raw products retrieved successfully, starting transformation")
                transformed_data = self.transform_product_data(raw_products)

                if transformed_data:
                    logger.info("Data transformation completed, saving to file")
                    self.save_to_file(transformed_data)
                    logger.info("Force sync operation completed successfully")
                    return transformed_data
                else:
                    logger.error("Data transformation failed")
                    return None
            else:
                logger.error("Failed to retrieve raw products")
                return None

        except Exception as e:
            logger.error(f"Error in force_sync: {e}")
            raise

    async def fetch_data(self):
        """Only read from saved file"""
        logger.info("Fetching data from saved file")
        return self.load_from_file()

async def main():
    """Main function for local debugging"""
    # Set up logging for console output
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    logger.info("Starting local debug for Coles V2 crawler with Scrapling")

    try:
        # Initialize crawler
        crawler = ColesV2Crawler()

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
