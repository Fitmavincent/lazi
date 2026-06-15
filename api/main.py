from fastapi import FastAPI, Query, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from services.service import Service
from services.special_crawler.oz_crawler import OzCrawler
from services.registry import (
    coles_crawler_service,
    coles_v2_crawler_service,
    coles_v2_5_crawler_service,
    woolies_crawler_service,
    chemist_warehouse_crawler_service,
    coles_refresh,
    woolies_refresh,
    chemist_warehouse_refresh,
)
from services.freshness import is_stale, freshness_report
from typing import Annotated
from scheduler import scheduler, setup_scheduler
from pydantic import BaseModel
import logging

service = Service()
oz_crawler_service = OzCrawler()

# Internal metadata fields added by the V2.5 crawler that must be stripped
# before returning to callers — the frozen API shape must not change.
_INTERNAL_FIELDS = {"crawl_status", "pages_attempted", "pages_succeeded", "pages_blocked", "crawler_version"}
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
async def shutdown_services():
    try:
        if scheduler.running:
            scheduler.shutdown(wait=False)
    except Exception as e:
        logger.warning(f"Scheduler shutdown error: {e}")
    try:
        await coles_refresh.shutdown()
        await woolies_refresh.shutdown()
        await chemist_warehouse_refresh.shutdown()
    except Exception as e:
        logger.warning(f"Refresh manager shutdown error: {e}")

@app.get("/")
def read_root():
    return {"message": "This is Vince API server."}

@app.get("/health")
async def read_health():
    """Freshness diagnostics live here ONLY — data endpoints' shape is frozen."""
    coles_data = await coles_v2_5_crawler_service.fetch_data()
    woolies_data = await woolies_crawler_service.fetch_data()
    cw_data = await chemist_warehouse_crawler_service.fetch_data()
    return {
        "status": "ok",
        "data_freshness": {
            "coles": freshness_report(coles_data) | coles_refresh.status(),
            "woolies": freshness_report(woolies_data) | woolies_refresh.status(),
            "chemist_warehouse": freshness_report(cw_data) | chemist_warehouse_refresh.status(),
        },
    }

@app.get("/calculate/{input}")
def read_calculate(input: int):
    return service.calculate(input)

@app.get("/oz-data")
def read_oz_data(page: int = 20, wish: Annotated[list[str] | None, Query()] = None):
    data = oz_crawler_service.oz_crawl_pipeline(page, wish)
    return data

@app.get("/coles-data")
async def read_coles_data():
    """Read from saved JSON file; trigger background re-crawl when stale."""
    data = await coles_crawler_service.fetch_data()
    coles_refresh.trigger_if_needed(is_stale(data))
    if not data:
        raise HTTPException(status_code=404, detail="No data available")
    return {k: v for k, v in data.items() if k not in _INTERNAL_FIELDS}

@app.post("/coles-data/sync")
async def force_sync_coles_data():
    """Force sync Coles data (routed to the V2.5 crawler)"""
    data = await coles_v2_5_crawler_service.force_sync()
    if not data:
        raise HTTPException(status_code=500, detail="Failed to sync data")
    return {"status": "success", "message": "Data synced successfully"}

@app.get("/coles-data-v2")
async def read_coles_data_v2():
    """Read from saved JSON file; trigger background re-crawl when stale."""
    data = await coles_v2_crawler_service.fetch_data()
    coles_refresh.trigger_if_needed(is_stale(data))
    if not data:
        raise HTTPException(status_code=404, detail="No data available")
    return {k: v for k, v in data.items() if k not in _INTERNAL_FIELDS}

@app.post("/coles-data-v2/sync")
async def force_sync_coles_data_v2():
    """Force sync Coles data (routed to the V2.5 crawler)"""
    data = await coles_v2_5_crawler_service.force_sync()
    if not data:
        raise HTTPException(status_code=500, detail="Failed to sync data")
    return {"status": "success", "message": "Data synced successfully"}

@app.get("/coles-data-v2-5")
async def read_coles_data_v2_5():
    """Read Coles half-price specials from R2; trigger background re-crawl when stale."""
    data = await coles_v2_5_crawler_service.fetch_data()
    coles_refresh.trigger_if_needed(is_stale(data))
    if not data:
        raise HTTPException(status_code=404, detail="No data available")
    return {k: v for k, v in data.items() if k not in _INTERNAL_FIELDS}

@app.post("/coles-data-v2-5/sync")
async def force_sync_coles_data_v2_5():
    """Force sync Coles half-price specials using the V2.5 crawler"""
    data = await coles_v2_5_crawler_service.force_sync()
    if not data:
        raise HTTPException(status_code=500, detail="Failed to sync data")
    return {"status": "success", "message": "Data synced successfully"}

@app.get("/woolies-data")
async def read_woolies_data():
    """Read from saved JSON file; trigger background re-crawl when stale."""
    data = await woolies_crawler_service.fetch_data()
    woolies_refresh.trigger_if_needed(is_stale(data))
    if not data:
        raise HTTPException(status_code=404, detail="No data available")
    return {k: v for k, v in data.items() if k not in _INTERNAL_FIELDS}

@app.post("/woolies-data/sync")
async def force_sync_woolies_data():
    """Force sync data from Woolworths"""
    data = await woolies_crawler_service.force_sync()
    if not data:
        raise HTTPException(status_code=500, detail="Failed to sync data")
    return {"status": "success", "message": "Data synced successfully"}

@app.get("/chemist-warehouse-data")
async def read_chemist_warehouse_data():
    """Read from saved JSON file; trigger background re-crawl when stale."""
    data = await chemist_warehouse_crawler_service.fetch_data()
    chemist_warehouse_refresh.trigger_if_needed(is_stale(data))
    if not data:
        raise HTTPException(status_code=404, detail="No data available")
    return {k: v for k, v in data.items() if k not in _INTERNAL_FIELDS}

@app.post("/chemist-warehouse-data/sync")
async def force_sync_chemist_warehouse_data():
    """Force sync data from Chemist Warehouse"""
    data = await chemist_warehouse_crawler_service.force_sync()
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

@app.get("/test/coles-crawl-v2-5")
async def test_coles_crawl_v2_5():
    """Test endpoint for the Coles V2.5 crawler without storage — limited to 2 pages"""
    original_max_pages = coles_v2_5_crawler_service.max_pages
    coles_v2_5_crawler_service.max_pages = 2
    try:
        result = await coles_v2_5_crawler_service.crawl_pipeline()
        return {
            "pagination_info": {
                "pages_attempted": result["pages_attempted"],
                "pages_succeeded": result["pages_succeeded"],
                "pages_blocked": result["pages_blocked"],
                "crawler_type": "Scrapling AsyncStealthySession",
            },
            "samples": result["data"][:5],
            "total_products": result["count"],
            "crawl_status": result["crawl_status"],
        }
    finally:
        coles_v2_5_crawler_service.max_pages = original_max_pages

@app.get("/test/woolies-crawl")
async def test_woolies_crawl():
    """Test endpoint for the Woolies crawler without storage — limited to 2 pages"""
    original_max_pages = woolies_crawler_service.max_pages
    woolies_crawler_service.max_pages = 2
    try:
        result = await woolies_crawler_service.crawl_pipeline()
        return {
            "pagination_info": {
                "pages_attempted": result["pages_attempted"],
                "pages_succeeded": result["pages_succeeded"],
                "pages_blocked": result["pages_blocked"],
                "crawler_type": "Scrapling AsyncStealthySession (XHR capture)",
            },
            "samples": result["data"][:5],
            "total_products": result["count"],
            "crawl_status": result["crawl_status"],
        }
    finally:
        woolies_crawler_service.max_pages = original_max_pages

@app.get("/test/chemist-warehouse-crawl")
async def test_chemist_warehouse_crawl():
    """Test endpoint for the Chemist Warehouse crawler without storage — limited to 2 pages"""
    original_max_pages = chemist_warehouse_crawler_service.max_pages
    chemist_warehouse_crawler_service.max_pages = 2
    try:
        result = await chemist_warehouse_crawler_service.crawl_pipeline()
        return {
            "pagination_info": {
                "pages_attempted": result["pages_attempted"],
                "pages_succeeded": result["pages_succeeded"],
                "pages_blocked": result["pages_blocked"],
                "crawler_type": "Scrapling AsyncStealthySession (Algolia XHR capture)",
            },
            "samples": result["data"][:5],
            "total_products": result["count"],
            "crawl_status": result["crawl_status"],
        }
    finally:
        chemist_warehouse_crawler_service.max_pages = original_max_pages
