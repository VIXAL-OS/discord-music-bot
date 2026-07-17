from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from music_event_bot.app import Application
from music_event_bot.config import Settings
from music_event_bot.discord.bot import MusicEventDiscordBot
from music_event_bot.discord.publishing import event_embed
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


class _ReviewRepoStub:
    def __init__(self, record: EventRecord, stored_hash: str | None) -> None:
        self.record = record
        self.stored_hash = stored_hash
        self.saved: list[tuple[str, int, int, str | None]] = []

    async def get_event(self, event_id: str) -> EventRecord:
        return self.record

    async def get_review_sync_state(self, event_id: str) -> tuple[int | None, str | None]:
        return 111, self.stored_hash

    async def find_nearby_venue_events(self, event: EventRecord) -> list[EventRecord]:
        return []

    async def set_review_message(
        self, event_id: str, channel_id: int, message_id: int, card_hash: str | None = None
    ) -> None:
        self.saved.append((event_id, channel_id, message_id, card_hash))


def _review_bot(repo: _ReviewRepoStub) -> MusicEventDiscordBot:
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
            repository=repo,
            discovery=SimpleNamespace(run=None),
            profile=SimpleNamespace(),
        ),
    )
    return MusicEventDiscordBot(app)


@pytest.mark.asyncio
async def test_sync_event_review_skips_unchanged_cards() -> None:
    from music_event_bot.discord.bot import review_card_hash
    from music_event_bot.discord.review import review_embed

    record = _record()
    repo = _ReviewRepoStub(record, stored_hash=None)
    bot = _review_bot(repo)
    try:
        current_hash = review_card_hash(review_embed(record), has_view=True)
        repo.stored_hash = current_hash

        async def fail_fetch(message_id: int) -> None:
            raise AssertionError("unchanged card must not be fetched or edited")

        async def fail_send(**kwargs: Any) -> None:
            raise AssertionError("unchanged card must not be re-sent")

        channel = SimpleNamespace(id=2, fetch_message=fail_fetch, send=fail_send)
        await bot.sync_event_review(record.id, channel=channel)  # type: ignore[arg-type]
        assert repo.saved == []
    finally:
        await bot.close()


@pytest.mark.asyncio
async def test_sync_event_review_edits_when_card_changed() -> None:
    record = _record()
    repo = _ReviewRepoStub(record, stored_hash="stale-hash")
    bot = _review_bot(repo)
    try:
        edits: list[dict[str, Any]] = []

        async def edit(**kwargs: Any) -> None:
            edits.append(kwargs)

        message = SimpleNamespace(id=111, edit=edit)

        async def fetch_message(message_id: int) -> SimpleNamespace:
            assert message_id == 111
            return message

        channel = SimpleNamespace(id=2, fetch_message=fetch_message)
        await bot.sync_event_review(record.id, channel=channel)  # type: ignore[arg-type]
        assert len(edits) == 1
        assert len(repo.saved) == 1
        assert repo.saved[0][3] is not None  # the fresh card hash was stored
    finally:
        await bot.close()


def test_orphan_card_detection() -> None:
    from music_event_bot.discord.bot import message_is_orphan_card

    registered = frozenset({111})
    footer = SimpleNamespace(
        text="[music-event-id:5297f194-d427-4ae8-9884-a5cd1e6ce5ec] • score 90/100"
    )
    card_embed = SimpleNamespace(footer=footer)

    canonical = SimpleNamespace(id=111, author=SimpleNamespace(id=42), embeds=[card_embed])
    orphan = SimpleNamespace(id=222, author=SimpleNamespace(id=42), embeds=[card_embed])
    other_author = SimpleNamespace(id=333, author=SimpleNamespace(id=7), embeds=[card_embed])
    chatter = SimpleNamespace(
        id=444, author=SimpleNamespace(id=42), embeds=[SimpleNamespace(footer=None)]
    )

    assert not message_is_orphan_card(canonical, 42, registered)  # type: ignore[arg-type]
    assert message_is_orphan_card(orphan, 42, registered)  # type: ignore[arg-type]
    assert not message_is_orphan_card(other_author, 42, registered)  # type: ignore[arg-type]
    assert not message_is_orphan_card(chatter, 42, registered)  # type: ignore[arg-type]


def test_full_description_is_kept_and_score_is_reviewer_only() -> None:
    description = "Doors 7 PM\n\nBag policy: small bags only.\n\nRain or shine."
    record = _record(description=description, artists=("Headliner",))

    review = event_embed(record, pending=True)
    assert review.description == description
    assert review.footer.text is not None
    assert "score 90/100" in review.footer.text
    assert "music-event-id:e1" in review.footer.text

    published = event_embed(record)
    assert published.description == description
    assert published.footer.text is not None
    assert "score" not in published.footer.text
    assert "music-event-id:e1" in published.footer.text


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

    async def fake_sync_event_review(event_id: str, *, channel: object = None) -> str:
        synced_ids.append(event_id)
        return "edited" if event_id in existing_messages else "posted"

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
