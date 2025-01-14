from bs4 import BeautifulSoup
from playwright.async_api import async_playwright, Route, Request
import random
import json
import asyncio

COLES_BASE_URL = "https://www.coles.com.au"
API_URL_PATTERN = "**/api/product*"

USER_AGENTS_LIST = [
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.2227.0 Safari/537.36',
    'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.3497.92 Safari/537.36',
    'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Safari/537.36',
]

class ColesCrawler:
    def __init__(self):
        self.special_api_response = None

    async def handle_request(self, route: Route, request: Request):
        print(f"Requesting: {route.request.url}")
        response = await route.fetch()
        self.special_api_response = await response.json()
        # print(f"Response: {self.special_api_response}")
        await route.continue_()

    async def crawl_coles_pipeline(self):
        async with async_playwright() as p:
            browser = await p.firefox.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1600, "height": 1200},
                user_agent=random.choice(USER_AGENTS_LIST)
            )

            page = await context.new_page()

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

    async def process_data(self):
        data = await self.crawl_coles_pipeline()
        if data:
            return data
        return None