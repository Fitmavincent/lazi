from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from services.special_crawler.coles_crawler import ColesCrawler
from services.special_crawler.woolies_crawler import WooliesCrawler
import asyncio
from datetime import datetime
import logging

# Set up logging with more detailed configuration
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('scheduler')

# Ensure the logger level is set (sometimes needed despite basicConfig)
logger.setLevel(logging.INFO)

# Create a global scheduler instance
scheduler = AsyncIOScheduler(
    timezone='Australia/Sydney',
    job_defaults={
        'coalesce': False,
        'max_instances': 1
    }
)

async def test_cron_job():
    logger.info(f"Test cron job started at {datetime.now()}")
    try:
        await asyncio.sleep(5)
        logger.info("Test job completed successfully")
        print(f"Test job executed at {datetime.now()}")  # Direct console output
        return {"status": "success"}
    except Exception as e:
        logger.error(f"Test job failed: {e}")
        return {"status": "failed"}

async def fetch_coles_data():
    logger.info(f"Running Coles crawler cron job at {datetime.now()}")
    crawler = ColesCrawler()
    raw_data = await crawler.crawl_coles_pipeline()
    if raw_data:
        transformed_data = crawler.transform_product_data(raw_data)
        crawler.save_to_file(transformed_data)
        logger.info(f"Coles crawler completed at {datetime.now()}")
        return {"status": "success"}
    return {"status": "failed"}

async def fetch_woolies_data():
    logger.info(f"Running Woolies crawler cron job at {datetime.now()}")
    crawler = WooliesCrawler()
    raw_data = await crawler.crawl_woolies_pipeline()
    if raw_data:
        transformed_data = crawler.transform_product_data(raw_data)
        crawler.save_to_file(transformed_data)
        logger.info(f"Woolies crawler completed at {datetime.now()}")
        return {"status": "success"}
    return {"status": "failed"}

def setup_scheduler():
    if not scheduler.running:
        logger.info("Setting up scheduler...")

        # Add test job
        # scheduler.add_job(
        #     test_cron_job,
        #     'interval',
        #     seconds=30,  # Changed to 30 seconds for easier testing
        #     id='test_cron_job',
        #     replace_existing=True,
        #     next_run_time=datetime.now()  # Start immediately
        # )

        # Add production jobs
        scheduler.add_job(
            fetch_coles_data,
            CronTrigger(
                day_of_week='wed',
                hour=0,
                minute=0,
                timezone='Australia/Sydney'
            ),
            id='fetch_coles_data',
            misfire_grace_time=None
        )

        scheduler.add_job(
            fetch_woolies_data,
            CronTrigger(
                day_of_week='wed',
                hour=0,
                minute=0,
                timezone='Australia/Sydney'
            ),
            id='fetch_woolies_data',
            misfire_grace_time=None
        )

        logger.info("Scheduler setup completed")
    return scheduler
