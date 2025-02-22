from fastapi import FastAPI, Query, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from services.service import Service
from services.special_crawler.oz_crawler import OzCrawler
from services.special_crawler.coles_crawler import ColesCrawler
from services.special_crawler.woolies_crawler import WooliesCrawler
from typing import Annotated
from scheduler import setup_scheduler
from pydantic import BaseModel

service = Service()
oz_crawler_service = OzCrawler()
coles_crawler_service = ColesCrawler()
woolies_crawler_service = WooliesCrawler()
app = FastAPI()

origins = [
    "https://vin-channel.netlify.app",
    "https://home.fitmavincent.dev"
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
    scheduler = setup_scheduler()
    scheduler.start()

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
