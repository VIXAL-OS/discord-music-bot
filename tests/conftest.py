from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import pytest_asyncio

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from music_event_bot.config import Settings  # noqa: E402
from music_event_bot.discovery.base import DiscoveryWindow  # noqa: E402
from music_event_bot.domain.models import DiscoveredEvent  # noqa: E402
from music_event_bot.storage.database import Database  # noqa: E402
from music_event_bot.storage.repositories import EventRepository  # noqa: E402


@pytest.fixture
def discovery_window() -> DiscoveryWindow:
    timezone = ZoneInfo("America/New_York")
    return DiscoveryWindow(
        starts_at=datetime(2026, 7, 1, 0, 0, tzinfo=timezone),
        ends_at=datetime(2026, 8, 1, 0, 0, tzinfo=timezone),
        default_timezone=timezone,
        default_event_duration_minutes=180,
    )


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, database_path=tmp_path / "events.sqlite3")


@pytest_asyncio.fixture
async def database(settings: Settings) -> Database:
    database = Database(settings.database_path)
    await database.initialize()
    return database


@pytest_asyncio.fixture
async def repository(database: Database) -> EventRepository:
    return EventRepository(database)


@pytest.fixture
def complete_event() -> DiscoveredEvent:
    return DiscoveredEvent(
        source_name="test-source",
        source_event_id="event-1",
        title="The Example Ensemble",
        artist="The Example Ensemble",
        venue="Example Hall",
        location="Example Hall, New York, NY",
        starts_at=datetime(2026, 7, 12, 20, 0, tzinfo=ZoneInfo("America/New_York")),
        ends_at=datetime(2026, 7, 12, 23, 0, tzinfo=ZoneInfo("America/New_York")),
        timezone="America/New_York",
        source_url="https://events.example.test/show",
        genres=("Indie",),
        description="A complete event used by service tests.",
    )
