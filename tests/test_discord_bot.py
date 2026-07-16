from __future__ import annotations

from types import SimpleNamespace
from typing import cast

import pytest

from music_event_bot.app import Application
from music_event_bot.config import Settings
from music_event_bot.discord.bot import MusicEventDiscordBot


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
