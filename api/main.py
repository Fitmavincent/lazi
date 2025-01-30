from fastapi import FastAPI, Query, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from services.service import Service
from services.oz_crawler import OzCrawler
from services.special_crawler.coles_crawler import ColesCrawler
from typing import Annotated


service = Service()
oz_crawler_service = OzCrawler()
coles_crawler_service = ColesCrawler()
app = FastAPI()

origins = [
    "https://vin-channel.netlify.app",
    "https://home.fitmavincent.com"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    data = await coles_crawler_service.process_data()
    return data