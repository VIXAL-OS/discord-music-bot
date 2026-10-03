from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

from music_event_bot.config import Settings

logger = logging.getLogger(__name__)

DISCOVERY_RETRY_DELAY = timedelta(minutes=30)
DISCOVERY_MAX_RETRIES = 3


class BotScheduler:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.scheduler = AsyncIOScheduler(timezone=settings.timezone)
        self._discover: Callable[[], Awaitable[object]] | None = None

    async def run_discovery(self, attempt: int = 0) -> None:
        """Run discovery, and schedule a retry when it fails.

        Discovery is a once-a-day cron job, so a run that raised used to mean a
        full day with no new events: a single transient "database is locked"
        on 2026-10-02 left every calendar entry added since the previous run
        unread until the next morning. A failure now gets a few spaced-out
        retries before giving up until the next scheduled run.
        """
        if self._discover is None:
            raise RuntimeError("run_discovery called before configure()")
        try:
            await self._discover()
        except Exception:
            if attempt >= DISCOVERY_MAX_RETRIES:
                logger.exception(
                    "Discovery failed on attempt %d; giving up until the next scheduled run",
                    attempt + 1,
                )
                return
            retry_at = datetime.now(self.settings.timezone) + DISCOVERY_RETRY_DELAY
            logger.exception(
                "Discovery failed on attempt %d; retrying at %s",
                attempt + 1,
                retry_at.isoformat(timespec="minutes"),
            )
            self.scheduler.add_job(
                self.run_discovery,
                DateTrigger(run_date=retry_at, timezone=self.settings.timezone),
                kwargs={"attempt": attempt + 1},
                id="discovery-retry",
                max_instances=1,
                replace_existing=True,
            )

    def configure(
        self,
        discover: Callable[[], Awaitable[object]],
        sync_reviews: Callable[[], Awaitable[object]],
        expire_events: Callable[[], Awaitable[object]],
        drain_publications: Callable[[], Awaitable[object]] | None = None,
        remind_rsvps: Callable[[], Awaitable[object]] | None = None,
        post_catchup: Callable[[], Awaitable[object]] | None = None,
    ) -> None:
        self._discover = discover
        self.scheduler.add_job(
            self.run_discovery,
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
        if remind_rsvps is not None:
            # Hourly with a 25-hour lookahead: each event is reminded once,
            # ~24 hours out, and missed windows catch up after downtime.
            self.scheduler.add_job(
                remind_rsvps,
                "interval",
                hours=1,
                id="rsvp-reminders",
                max_instances=1,
                coalesce=True,
                replace_existing=True,
            )
        if post_catchup is not None:
            # Once a day, early evening: late enough that the day's queue is
            # full, early enough to still be useful for tomorrow's shows.
            self.scheduler.add_job(
                post_catchup,
                "cron",
                hour=self.settings.catchup_hour,
                minute=11,
                id="catchup-post",
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
