from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from services.registry import (
    coles_v2_5_crawler_service,
    woolies_crawler_service,
    chemist_warehouse_crawler_service,
    priceline_crawler_service,
    coles_refresh,
    woolies_refresh,
    chemist_warehouse_refresh,
    priceline_refresh,
)
from services.freshness import is_stale
from datetime import datetime
import logging

# NOTE: This scheduler only fires while the Fly.io machine happens to be
# awake (min_machines_running = 0, auto_stop_machines = true). The primary
# refresh path is the stale-data trigger on the GET data endpoints — see
# services/refresh_manager.py. These jobs are a bonus when the machine is up.

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('scheduler')
logger.setLevel(logging.INFO)

scheduler = AsyncIOScheduler(
    timezone='Australia/Sydney',
    job_defaults={
        'coalesce': False,
        'max_instances': 1
    }
)

async def fetch_coles_data_v2_5():
    """Wednesday crawl — goes through the shared RefreshManager so it can
    never run concurrently with a fetch-triggered refresh."""
    logger.info(f"Cron: Coles V2.5 crawl at {datetime.now()}")
    started = coles_refresh.trigger_if_needed(stale=True)
    if not started:
        logger.info("Cron: Coles refresh already running or cooling down — skipped")

async def fetch_woolies_data():
    logger.info(f"Cron: Woolies crawl at {datetime.now()}")
    started = woolies_refresh.trigger_if_needed(stale=True)
    if not started:
        logger.info("Cron: Woolies refresh already running or cooling down — skipped")

async def fetch_chemist_warehouse_data():
    logger.info(f"Cron: Chemist Warehouse crawl at {datetime.now()}")
    started = chemist_warehouse_refresh.trigger_if_needed(stale=True)
    if not started:
        logger.info("Cron: Chemist Warehouse refresh already running or cooling down — skipped")

async def fetch_priceline_data():
    logger.info(f"Cron: Priceline crawl at {datetime.now()}")
    started = priceline_refresh.trigger_if_needed(stale=True)
    if not started:
        logger.info("Cron: Priceline refresh already running or cooling down — skipped")

async def conditional_retry():
    """Wednesday 06:00 — re-crawl only if the midnight run failed or never ran."""
    logger.info(f"Cron: conditional retry check at {datetime.now()}")
    coles_data = await coles_v2_5_crawler_service.fetch_data()
    coles_refresh.trigger_if_needed(is_stale(coles_data))
    woolies_data = await woolies_crawler_service.fetch_data()
    woolies_refresh.trigger_if_needed(is_stale(woolies_data))
    cw_data = await chemist_warehouse_crawler_service.fetch_data()
    chemist_warehouse_refresh.trigger_if_needed(is_stale(cw_data))
    priceline_data = await priceline_crawler_service.fetch_data()
    priceline_refresh.trigger_if_needed(is_stale(priceline_data))

def setup_scheduler():
    if not scheduler.running:
        logger.info("Setting up scheduler...")

        scheduler.add_job(
            fetch_woolies_data,
            CronTrigger(day_of_week='wed', hour=0, minute=5, timezone='Australia/Sydney'),
            id='fetch_woolies_data',
            misfire_grace_time=None
        )

        scheduler.add_job(
            fetch_coles_data_v2_5,
            CronTrigger(day_of_week='wed', hour=0, minute=15, timezone='Australia/Sydney'),
            id='fetch_coles_data_v2_5',
            misfire_grace_time=None
        )

        scheduler.add_job(
            fetch_chemist_warehouse_data,
            CronTrigger(day_of_week='wed', hour=0, minute=25, timezone='Australia/Sydney'),
            id='fetch_chemist_warehouse_data',
            misfire_grace_time=None
        )

        scheduler.add_job(
            fetch_priceline_data,
            CronTrigger(day_of_week='wed', hour=0, minute=35, timezone='Australia/Sydney'),
            id='fetch_priceline_data',
            misfire_grace_time=None
        )

        scheduler.add_job(
            conditional_retry,
            CronTrigger(day_of_week='wed', hour=6, minute=0, timezone='Australia/Sydney'),
            id='conditional_retry',
            misfire_grace_time=None
        )

        logger.info("Scheduler setup completed")
    return scheduler
