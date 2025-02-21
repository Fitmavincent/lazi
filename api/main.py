from fastapi import FastAPI, Query, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from services.service import Service
from services.oz_crawler import OzCrawler
from services.special_crawler.coles_crawler import ColesCrawler
from typing import Annotated
from scheduler import setup_scheduler
from pydantic import BaseModel

service = Service()
oz_crawler_service = OzCrawler()
coles_crawler_service = ColesCrawler()
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


class PasswordRequest(BaseModel):
    say: str

@app.post("/coles-data/sync/password")
async def can_force_sync(request: PasswordRequest):
    if request.say != "I am solemnly swear that I am up to no good":
        raise HTTPException(status_code=403, detail="Tsk Tsk! Nice try")
    return {"status": "success", "message": "Password validated"}
