from fastapi import FastAPI, Query, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from services.service import Service
from services.oz_crawler import OzCrawler
from typing import List



service = Service()
oz_crawler_service = OzCrawler()
app = FastAPI()

origins = [
    "https://vin-channel.netlify.app"
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
def read_oz_data(page: int = 20, wish: List[str] = Query(None)):
    data = oz_crawler_service.oz_crawl_pipeline(page, wish)
    return data