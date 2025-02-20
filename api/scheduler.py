from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from services.special_crawler.coles_crawler import ColesCrawler

async def fetch_coles_data():
    crawler = ColesCrawler()
    raw_data = await crawler.crawl_coles_pipeline()
    if raw_data:
        transformed_data = crawler.transform_product_data(raw_data)
        crawler.save_to_file(transformed_data)
        return {"status": "success"}
    return {"status": "failed"}

def setup_scheduler():
    scheduler = AsyncIOScheduler()

    # Schedule for Wednesday at 00:00 AM UTC+10
    scheduler.add_job(
        fetch_coles_data,
        CronTrigger(
            day_of_week='wed',
            hour=0,
            minute=0,
            timezone='Australia/Sydney'
        ),
        id='fetch_coles_data'
    )

    return scheduler
