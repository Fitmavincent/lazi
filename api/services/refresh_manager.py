"""
Stale-data refresh triggering.

The Fly.io machine sleeps between requests (min_machines_running = 0), so the
in-process APScheduler cron rarely fires. Instead, freshness is checked on the
data-read path: when the frontend fetches the weekly JSON and the stored data
predates this week's Wednesday-midnight specials reset, a background re-crawl
is kicked off on the machine that the request just woke up. The request itself
returns the existing (stale) data immediately — the crawl fills R2 for
subsequent fetches.

Guard rails:
  - one crawl at a time per retailer (in-process task handle)
  - cooldown between attempts so a blocked/failing crawler isn't hammered
    on every fetch
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

DEFAULT_COOLDOWN_SECONDS = 30 * 60


class RefreshManager:
    def __init__(
        self,
        name: str,
        sync_fn: Callable[[], Awaitable],
        cooldown_seconds: int = DEFAULT_COOLDOWN_SECONDS,
    ):
        self.name = name
        self._sync_fn = sync_fn
        self._cooldown = cooldown_seconds
        self._task: asyncio.Task | None = None
        self._last_attempt: float = 0.0

    @property
    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict:
        return {
            "refresh_in_progress": self.is_running,
            "last_attempt_age_seconds": round(time.monotonic() - self._last_attempt) if self._last_attempt else None,
            "cooldown_seconds": self._cooldown,
        }

    def trigger_if_needed(self, stale: bool) -> bool:
        """Start a background refresh when data is stale. Returns True if a
        refresh was started by this call."""
        if not stale:
            return False
        if self.is_running:
            logger.info(f"[{self.name}] refresh already in progress — not triggering another")
            return False
        if self._last_attempt and (time.monotonic() - self._last_attempt) < self._cooldown:
            logger.info(f"[{self.name}] refresh attempted recently — cooling down")
            return False

        self._last_attempt = time.monotonic()
        self._task = asyncio.create_task(self._run(), name=f"refresh-{self.name}")
        logger.info(f"[{self.name}] stale data detected — background refresh started")
        return True

    async def _run(self):
        try:
            result = await self._sync_fn()
            if result:
                logger.info(f"[{self.name}] background refresh completed successfully")
            else:
                logger.warning(f"[{self.name}] background refresh finished without new data (crawl failed/blocked)")
        except asyncio.CancelledError:
            logger.warning(f"[{self.name}] background refresh cancelled (likely machine shutdown)")
            raise
        except Exception:
            logger.exception(f"[{self.name}] background refresh raised")

    async def shutdown(self):
        if self.is_running:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
