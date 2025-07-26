from fastapi import FastAPI, Query, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from services.service import Service
from services.special_crawler.oz_crawler import OzCrawler
from services.special_crawler.coles_crawler import ColesCrawler
from services.special_crawler.coles_crawler_v2 import ColesV2Crawler
from services.special_crawler.woolies_crawler import WooliesCrawler
from typing import Annotated
from scheduler import scheduler, setup_scheduler
from pydantic import BaseModel
import logging

service = Service()
oz_crawler_service = OzCrawler()
coles_crawler_service = ColesCrawler()
coles_v2_crawler_service = ColesV2Crawler()
woolies_crawler_service = WooliesCrawler()
app = FastAPI()

logger = logging.getLogger(__name__)

origins = [
    "https://vin-channel.netlify.app",
    "https://home.fitmavincent.dev",
    "http://localhost:3000",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def start_scheduler():
    try:
        setup_scheduler()
        scheduler.start()
        logger.info("Scheduler started successfully")
    except Exception as e:
        logger.error(f"Failed to start scheduler: {e}")

@app.on_event("shutdown")
async def shutdown_scheduler():
    if scheduler.running:
        scheduler.shutdown()

@app.get("/")
def read_root():
    return {"message": "This is Vince API server."}

@app.get("/health")
def read_health():
    return {"status": "ok"}

@app.get("/calculate/{input}")
def read_calculate(input: int):
    return service.calculate(input)

@app.get("/oz-data")
def read_oz_data(page: int = 20, wish: Annotated[list[str] | None, Query()] = None):
    data = oz_crawler_service.oz_crawl_pipeline(page, wish)
    return data

@app.get("/coles-data")
async def read_coles_data():
    """Read from saved JSON file"""
    data = await coles_crawler_service.fetch_data()
    if not data:
        raise HTTPException(status_code=404, detail="No data available")
    return data

@app.post("/coles-data/sync")
async def force_sync_coles_data():
    """Force sync data from Coles API"""
    data = await coles_crawler_service.force_sync()
    if not data:
        raise HTTPException(status_code=500, detail="Failed to sync data")
    return {"status": "success", "message": "Data synced successfully"}

@app.get("/coles-data-v2")
async def read_coles_data_v2():
    """Read from saved JSON file (V2 Scrapling crawler)"""
    data = await coles_v2_crawler_service.fetch_data()
    if not data:
        raise HTTPException(status_code=404, detail="No data available")
    return data

@app.post("/coles-data-v2/sync")
async def force_sync_coles_data_v2():
    """Force sync data from Coles website using Scrapling (V2)"""
    data = await coles_v2_crawler_service.force_sync()
    if not data:
        raise HTTPException(status_code=500, detail="Failed to sync data")
    return {"status": "success", "message": "Data synced successfully"}

@app.get("/woolies-data")
async def read_woolies_data():
    """Read from saved JSON file"""
    data = await woolies_crawler_service.fetch_data()
    if not data:
        raise HTTPException(status_code=404, detail="No data available")
    return data

@app.post("/woolies-data/sync")
async def force_sync_woolies_data():
    """Force sync data from Woolworths"""
    data = await woolies_crawler_service.force_sync()
    if not data:
        raise HTTPException(status_code=500, detail="Failed to sync data")
    return {"status": "success", "message": "Data synced successfully"}

class PasswordRequest(BaseModel):
    say: str

@app.post("/coles-data/sync/password")
async def can_force_sync(request: PasswordRequest):
    if request.say != "I am solemnly swear that I am up to no good":
        raise HTTPException(status_code=403, detail="Tsk Tsk! Nice try")
    return {"status": "success", "message": "Password validated"}

@app.get("/test/coles-crawl")
async def test_coles_crawl():
    """Test endpoint for Coles crawler without storage"""
    raw_data = await coles_crawler_service.crawl_coles_pipeline()
    if not raw_data:
        raise HTTPException(status_code=500, detail="Failed to fetch Coles data")
    transformed_data = coles_crawler_service.transform_product_data(raw_data)
    return {
        "raw_samples": raw_data['results'][:5] if raw_data['results'] else None,
        "transformed_samples": transformed_data['data'][:5] if transformed_data['data'] else None,
        "total_products": len(transformed_data['data'])
    }

@app.get("/test/woolies-crawl")
async def test_woolies_crawl():
    """Test endpoint for Woolworths crawler without storage"""
    # Set to 3 pages for testing
    woolies_crawler_service.max_pages = 3

    raw_data = await woolies_crawler_service.crawl_woolies_pipeline()
    if not raw_data:
        raise HTTPException(status_code=500, detail="Failed to fetch Woolworths data")

    products = raw_data['products']
    transformed_data = woolies_crawler_service.transform_product_data(products)

    return {
        "pagination_info": {
            "pages_fetched": raw_data['pagination'],
            "total_pages_attempted": woolies_crawler_service.current_page,
            "max_pages_limit": woolies_crawler_service.max_pages,
            "products_per_page": 36,
            "actual_products_found": len(products)
        },
        "raw_samples": products[:5] if products else None,
        "transformed_samples": transformed_data['data'][:5] if transformed_data['data'] else None,
        "total_products": len(transformed_data['data']),
        "total_half_price_products": len([p for p in products if p.get('price_was')])
    }

@app.get("/test/coles-crawl-v2")
async def test_coles_crawl_v2():
    """Test endpoint for Coles V2 crawler (Scrapling) without storage - limited to 3 pages for testing"""
    # Temporarily set to 3 pages for testing
    original_max_pages = coles_v2_crawler_service.max_pages
    coles_v2_crawler_service.max_pages = 3

    try:
        raw_products = await coles_v2_crawler_service.crawl_coles_pipeline()
        if not raw_products:
            raise HTTPException(status_code=500, detail="Failed to fetch Coles V2 data")

        transformed_data = coles_v2_crawler_service.transform_product_data(raw_products)

        return {
            "pagination_info": {
                "pages_attempted": coles_v2_crawler_service.max_pages,
                "products_found": len(raw_products),
                "crawler_type": "Scrapling StealthyFetcher",
                "max_pages_production": original_max_pages
            },
            "raw_samples": raw_products[:5] if raw_products else None,
            "transformed_samples": transformed_data['data'][:5] if transformed_data['data'] else None,
            "total_products": len(transformed_data['data']),
            "sample_product_details": {
                "has_prices": len([p for p in raw_products if p.get('price', 0) > 0]),
                "has_discounts": len([p for p in raw_products if p.get('discount')]),
                "has_images": len([p for p in raw_products if p.get('image')]),
                "has_links": len([p for p in raw_products if p.get('product_link')])
            }
        }
    finally:
        # Restore original max_pages
        coles_v2_crawler_service.max_pages = original_max_pages
