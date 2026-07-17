from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

from music_event_bot.config import Settings

logger = logging.getLogger(__name__)


class BotScheduler:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.scheduler = AsyncIOScheduler(timezone=settings.timezone)

    def configure(
        self,
        discover: Callable[[], Awaitable[object]],
        sync_reviews: Callable[[], Awaitable[object]],
        expire_events: Callable[[], Awaitable[object]],
        drain_publications: Callable[[], Awaitable[object]] | None = None,
    ) -> None:
        self.scheduler.add_job(
            discover,
            CronTrigger.from_crontab(self.settings.discovery_cron, timezone=self.settings.timezone),
            id="discovery",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        self.scheduler.add_job(
            sync_reviews,
            "interval",
            minutes=self.settings.review_sync_interval_minutes,
            id="review-sync",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        self.scheduler.add_job(
            expire_events,
            "cron",
            hour=4,
            minute=23,
            id="expire-events",
            max_instances=1,
            coalesce=True,
            replace_existing=True,
        )
        if drain_publications is not None:
            self.scheduler.add_job(
                drain_publications,
                "interval",
                minutes=10,
                id="publish-drain",
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )

    def start(self) -> None:
        if not self.scheduler.running:
            self.scheduler.start()
            logger.info("Scheduler started")

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
