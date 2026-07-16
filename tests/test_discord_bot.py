from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from music_event_bot.app import Application
from music_event_bot.config import Settings
from music_event_bot.discord.bot import MusicEventDiscordBot
from music_event_bot.discord.publishing import _description_summary, event_embed
from music_event_bot.domain.models import EventRecord, EventStatus


def _record(**overrides: Any) -> EventRecord:
    values: dict[str, Any] = {
        "id": "e1",
        "title": "Show",
        "artist": "Headliner",
        "artists": ("Headliner", "Opener One", "Opener Two"),
        "venue": "Hall",
        "location": "Hall, Pittsburgh, PA",
        "starts_at": datetime(2026, 8, 1, 23, 0, tzinfo=UTC),
        "ends_at": None,
        "timezone": "America/New_York",
        "url": "https://tickets.example.test/1",
        "image_url": None,
        "description": None,
        "genres": ("metal",),
        "status": EventStatus.PENDING_REVIEW,
        "score": 90,
        "match_reasons": ("artist match: Headliner",),
        "created_at": datetime(2026, 7, 16, tzinfo=UTC),
        "updated_at": datetime(2026, 7, 16, tzinfo=UTC),
    }
    values.update(overrides)
    return EventRecord(**values)


def test_long_descriptions_are_summarized() -> None:
    summary = _description_summary("policy line\n\n" + "x" * 1000)
    assert len(summary) <= 350
    assert summary.endswith("…")
    assert "\n" not in summary


def test_lineup_field_shows_full_bill() -> None:
    embed = event_embed(_record(), pending=True)
    fields = {field.name: field.value for field in embed.fields}
    assert fields["Lineup"] == "Headliner, Opener One, Opener Two"
    assert "Artist" not in fields


def test_single_artist_keeps_artist_field() -> None:
    embed = event_embed(_record(artists=("Headliner",)), pending=True)
    fields = {field.name: field.value for field in embed.fields}
    assert fields["Artist"] == "Headliner"
    assert "Lineup" not in fields


@pytest.mark.asyncio
async def test_bot_uses_non_reserved_application_state_attribute() -> None:
    settings = Settings(
        _env_file=None,
        discord_token="test-token",
        discord_guild_id=1,
        review_channel_id=2,
        announcement_channel_id=3,
        admin_user_ids="4",
    )
    app = cast(
        Application,
        SimpleNamespace(
            settings=settings,
            repository=SimpleNamespace(),
            discovery=SimpleNamespace(run=None),
            profile=SimpleNamespace(),
        ),
    )

    bot = MusicEventDiscordBot(app)
    try:
        assert bot.music_app is app
        assert bot.application is None
    finally:
        await bot.close()


@pytest.mark.asyncio
async def test_sync_reviews_caps_new_posts_per_cycle() -> None:
    settings = Settings(
        _env_file=None,
        discord_token="test-token",
        discord_guild_id=1,
        review_channel_id=2,
        announcement_channel_id=3,
        admin_user_ids="4",
        review_post_batch_size=2,
    )
    queue = [SimpleNamespace(id=f"event-{i}") for i in range(4)]
    existing_messages = {"event-3": 111}

    async def list_review_queue() -> list[SimpleNamespace]:
        return queue

    async def get_review_message_id(event_id: str) -> int | None:
        return existing_messages.get(event_id)

    app = cast(
        Application,
        SimpleNamespace(
            settings=settings,
            repository=SimpleNamespace(
                list_review_queue=list_review_queue,
                get_review_message_id=get_review_message_id,
            ),
            discovery=SimpleNamespace(run=None),
            profile=SimpleNamespace(),
        ),
    )
    bot = MusicEventDiscordBot(app)
    synced_ids: list[str] = []

    async def fake_sync_event_review(event_id: str, *, channel: object = None) -> None:
        synced_ids.append(event_id)

    async def fake_review_channel() -> object:
        return object()

    bot.sync_event_review = fake_sync_event_review  # type: ignore[method-assign]
    bot._review_channel = fake_review_channel  # type: ignore[method-assign]
    try:
        synced = await bot.sync_reviews()
    finally:
        await bot.close()

    # Two new posts (score order), the over-budget new events skipped, and the
    # already-posted card still refreshed.
    assert synced_ids == ["event-0", "event-1", "event-3"]
    assert synced == 3
