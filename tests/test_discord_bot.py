from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast

import pytest

from music_event_bot.app import Application
from music_event_bot.config import Settings
from music_event_bot.discord.bot import MusicEventDiscordBot
from music_event_bot.discord.publishing import event_embed
from music_event_bot.domain.blocklist import Blocklist
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
        self.cleared: list[str] = []

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

    async def clear_review_message(self, event_id: str) -> None:
        self.cleared.append(event_id)


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
            blocklist=Blocklist(),
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


@pytest.mark.asyncio
async def test_decided_event_card_is_deleted_not_edited() -> None:
    record = _record(status=EventStatus.REJECTED)
    repo = _ReviewRepoStub(record, stored_hash="whatever")
    bot = _review_bot(repo)
    deleted: list[int] = []

    async def delete() -> None:
        deleted.append(111)

    async def fail_edit(**kwargs: Any) -> None:
        raise AssertionError("decided cards are deleted, never edited")

    message = SimpleNamespace(id=111, delete=delete, edit=fail_edit)

    async def fetch_message(message_id: int) -> SimpleNamespace:
        assert message_id == 111
        return message

    channel = SimpleNamespace(id=2, fetch_message=fetch_message)
    try:
        result = await bot.sync_event_review(record.id, channel=channel)  # type: ignore[arg-type]
    finally:
        await bot.close()

    assert result == "removed"
    assert deleted == [111]
    assert repo.cleared == ["e1"]
    assert repo.saved == []


def test_chunk_message_lines_packs_under_discord_cap() -> None:
    from music_event_bot.discord.bot import chunk_message_lines

    assert chunk_message_lines([]) == []
    lines = [f"- event {i} " + "x" * 60 for i in range(200)]
    chunks = chunk_message_lines(lines)
    assert len(chunks) > 1
    assert all(len(chunk) <= 1900 for chunk in chunks)
    # Nothing dropped, order preserved.
    assert "\n".join(chunks) == "\n".join(lines)


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
            blocklist=Blocklist(),
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

    async def list_departed_events_with_cards() -> list[SimpleNamespace]:
        return []

    async def get_review_message_id(event_id: str) -> int | None:
        return existing_messages.get(event_id)

    app = cast(
        Application,
        SimpleNamespace(
            settings=settings,
            repository=SimpleNamespace(
                list_review_queue=list_review_queue,
                list_departed_events_with_cards=list_departed_events_with_cards,
                get_review_message_id=get_review_message_id,
            ),
            discovery=SimpleNamespace(run=None),
            profile=SimpleNamespace(),
            blocklist=Blocklist(),
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


class _CatchupRepoStub:
    def __init__(self, queued: list[tuple[EventRecord, list[int]]]) -> None:
        self.queued = queued
        self.delivered: tuple[str, ...] = ()
        self.expired_at: datetime | None = None

    async def expire_stale_queued(self, now: datetime) -> int:
        self.expired_at = now
        return 0

    async def list_queued_notifications(self) -> list[tuple[EventRecord, list[int]]]:
        return self.queued

    async def get_publication(self, event_id: str) -> dict[str, str]:
        return {"announcement_message_id": "900", "announcement_channel_id": "3"}

    async def mark_notifications_delivered(self, event_ids: tuple[str, ...]) -> None:
        self.delivered = event_ids


def _catchup_bot(repo: _CatchupRepoStub, **overrides: Any) -> MusicEventDiscordBot:
    settings = Settings(
        _env_file=None,
        discord_token="test-token",
        discord_guild_id=1,
        review_channel_id=2,
        announcement_channel_id=3,
        admin_user_ids="4",
        **{"personal_delivery": "on", **overrides},
    )
    app = cast(
        Application,
        SimpleNamespace(
            settings=settings,
            repository=repo,
            discovery=SimpleNamespace(run=None),
            profile=SimpleNamespace(),
            blocklist=Blocklist(),
        ),
    )
    return MusicEventDiscordBot(app)


class _FakeChannel:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []

    async def send(self, content: str, **kwargs: Any) -> None:
        self.sent.append((content, kwargs))


@pytest.mark.asyncio
async def test_catchup_is_one_message_naming_every_overflowed_member(monkeypatch) -> None:
    """A member who overflowed by six shows should pay one notification for
    them, not six."""
    import music_event_bot.discord.bot as bot_module

    queued = [
        (_record(id="a", title="Liturgy"), [7]),
        (_record(id="b", title="Chat Pile"), [7, 8]),
    ]
    repo = _CatchupRepoStub(queued)
    bot = _catchup_bot(repo)
    channel = _FakeChannel()
    monkeypatch.setattr(bot_module.discord, "TextChannel", _FakeChannel)
    monkeypatch.setattr(bot, "get_channel", lambda _id: channel)

    sent = await bot.post_catchup()

    assert sent == 2
    assert len(channel.sent) == 1
    content, kwargs = channel.sent[0]
    assert "Liturgy" in content and "Chat Pile" in content
    assert "<@7>" in content and "<@8>" in content
    # Jump links point at the card's own channel, not the catch-up's.
    assert "/3/900" in content
    assert {obj.id for obj in kwargs["allowed_mentions"].users} == {7, 8}
    assert kwargs["allowed_mentions"].roles is False
    assert repo.delivered == ("a", "b")


@pytest.mark.asyncio
async def test_catchup_truncates_a_long_queue_rather_than_splitting(monkeypatch) -> None:
    import music_event_bot.discord.bot as bot_module

    queued = [(_record(id=f"e{n}", title=f"Show {n}"), [7]) for n in range(20)]
    repo = _CatchupRepoStub(queued)
    bot = _catchup_bot(repo, catchup_max_events=5)
    channel = _FakeChannel()
    monkeypatch.setattr(bot_module.discord, "TextChannel", _FakeChannel)
    monkeypatch.setattr(bot, "get_channel", lambda _id: channel)

    sent = await bot.post_catchup()

    assert sent == 5
    assert len(channel.sent) == 1
    assert "and 15 more waiting in the channel" in channel.sent[0][0]
    assert repo.delivered == tuple(f"e{n}" for n in range(5))


@pytest.mark.asyncio
async def test_catchup_does_nothing_while_delivery_is_not_on(monkeypatch) -> None:
    repo = _CatchupRepoStub([(_record(id="a"), [7])])
    bot = _catchup_bot(repo, personal_delivery="shadow")
    assert await bot.post_catchup() == 0
    assert repo.expired_at is None


def _profile_bot(repository: Any, **overrides: Any) -> MusicEventDiscordBot:
    settings = Settings(
        _env_file=None,
        discord_token="test-token",
        discord_guild_id=1,
        review_channel_id=2,
        announcement_channel_id=3,
        admin_user_ids="4",
        genre_role_map='{"goth": "10", "punk": "11"}',
        **overrides,
    )
    app = cast(
        Application,
        SimpleNamespace(
            settings=settings,
            repository=repository,
            discovery=SimpleNamespace(run=None),
            profile=SimpleNamespace(),
            blocklist=Blocklist(),
        ),
    )
    return MusicEventDiscordBot(app)


def _fake_member(user_id: int, name: str, *role_ids: int) -> Any:
    return SimpleNamespace(
        id=user_id,
        display_name=name,
        bot=False,
        roles=[SimpleNamespace(id=role_id) for role_id in role_ids],
    )


@pytest.mark.asyncio
async def test_ensure_profile_seeds_from_the_roles_a_member_holds(repository) -> None:
    await repository.seed_genre_roles({"goth": 10, "punk": 11, "darkwave": 10})
    bot = _profile_bot(repository)

    profile = await bot.ensure_profile(_fake_member(7, "Avery", 10))

    assert profile is not None
    assert profile["metro"] == "pittsburgh"
    assert profile["travel_band"] == "road-trip"
    assert profile["customized_at"] is None
    # The derived alias is not seeded, only the configured bucket.
    assert await repository.get_user_taste(7, "genre") == {"goth": 1}


@pytest.mark.asyncio
async def test_ensure_profile_is_idempotent(repository) -> None:
    await repository.seed_genre_roles({"goth": 10})
    bot = _profile_bot(repository)
    member = _fake_member(7, "Avery", 10)
    first = await bot.ensure_profile(member)
    await repository.update_user_profile(7, daily_ping_cap=2)

    second = await bot.ensure_profile(member)

    assert first is not None and second is not None
    assert second["daily_ping_cap"] == 2


@pytest.mark.asyncio
async def test_a_member_with_no_genre_roles_gets_a_profile_only_when_they_ask(
    repository,
) -> None:
    await repository.seed_genre_roles({"goth": 10})
    bot = _profile_bot(repository)
    lurker = _fake_member(8, "Lurker", 99)

    assert await bot.ensure_profile(lurker) is None
    assert await bot.ensure_profile(lurker, create_if_empty=True) is not None
    assert await repository.get_user_taste(8, "genre") == {}


@pytest.mark.asyncio
async def test_profile_summary_names_held_and_dropped_genres(repository) -> None:
    await repository.seed_genre_roles({"goth": 10, "punk": 11})
    bot = _profile_bot(repository)
    await bot.ensure_profile(_fake_member(7, "Avery", 10, 11))
    await repository.set_user_taste(7, "genre", "punk", -1)
    await repository.update_user_profile(7, metro="cleveland", travel_band="in-town")

    summary = await bot.profile_summary(7)

    assert "Cleveland" in summary
    assert "In town" in summary and "40 miles" in summary
    assert "Genres: goth" in summary
    assert "Dropped: punk" in summary


@pytest.mark.asyncio
async def test_profile_summary_says_no_limit_for_an_uncapped_member(repository) -> None:
    await repository.seed_genre_roles({"goth": 10})
    bot = _profile_bot(repository)
    await bot.ensure_profile(_fake_member(7, "Avery", 10))
    await repository.update_user_profile(7, daily_ping_cap=0)

    summary = await bot.profile_summary(7)

    assert "no limit" in summary and "catch-up" not in summary


@pytest.mark.asyncio
async def test_gaining_a_genre_role_seeds_a_profile(repository) -> None:
    """Without this, everyone who joins after the seed run holds roles that
    no longer drive delivery and matches nothing."""
    await repository.seed_genre_roles({"goth": 10, "punk": 11})
    bot = _profile_bot(repository)

    await bot.on_member_update(_fake_member(7, "Avery"), _fake_member(7, "Avery", 10))

    assert await repository.get_user_taste(7, "genre") == {"goth": 1}


@pytest.mark.asyncio
async def test_gaining_a_second_role_adds_its_genre(repository) -> None:
    await repository.seed_genre_roles({"goth": 10, "punk": 11})
    bot = _profile_bot(repository)
    await bot.ensure_profile(_fake_member(7, "Avery", 10))

    await bot.on_member_update(
        _fake_member(7, "Avery", 10), _fake_member(7, "Avery", 10, 11)
    )

    assert await repository.get_user_taste(7, "genre") == {"goth": 1, "punk": 1}


@pytest.mark.asyncio
async def test_gaining_an_unrelated_role_changes_nothing(repository) -> None:
    await repository.seed_genre_roles({"goth": 10})
    bot = _profile_bot(repository)

    await bot.on_member_update(_fake_member(7, "Avery"), _fake_member(7, "Avery", 99))

    assert await repository.get_user_profile(7) is None


@pytest.mark.asyncio
async def test_a_role_cannot_re_add_a_genre_the_member_dropped(repository) -> None:
    """A hand-tuned profile is the member's own. Re-joining a role they left
    must not undo the drop."""
    await repository.seed_genre_roles({"goth": 10, "punk": 11})
    bot = _profile_bot(repository)
    await bot.ensure_profile(_fake_member(7, "Avery", 10, 11))
    await repository.set_user_taste(7, "genre", "punk", -1)
    await repository.update_user_profile(7, daily_ping_cap=3)

    await bot.on_member_update(
        _fake_member(7, "Avery", 10), _fake_member(7, "Avery", 10, 11)
    )

    assert await repository.get_user_taste(7, "genre") == {"goth": 1, "punk": -1}
    assert await repository.list_user_taste("genre") == {7: {"goth"}}


@pytest.mark.asyncio
async def test_a_customized_profile_is_skipped_by_a_later_seed_run(repository) -> None:
    """update_user_profile stamps customized_at, which is the hook the
    seeder already respects."""
    from music_event_bot.services.profiles import (
        SKIPPED_CUSTOMIZED,
        GuildMember,
        plan_role_seed,
    )

    await repository.seed_genre_roles({"goth": 10, "punk": 11})
    bot = _profile_bot(repository)
    await bot.ensure_profile(_fake_member(7, "Avery", 10))
    await repository.update_user_profile(7, travel_band="in-town")

    actions = plan_role_seed(
        [GuildMember(user_id=7, display_name="Avery", role_ids=(10, 11))],
        {10: ("goth",), 11: ("punk",)},
        await repository.list_user_profiles(),
        await repository.list_user_taste("genre"),
        default_metro="pittsburgh",
    )

    assert actions[0].action == SKIPPED_CUSTOMIZED
    assert actions[0].writes is False


def test_help_lists_every_me_subcommand_for_anyone() -> None:
    text = _profile_bot(None)._help_text(reviewer=False)
    for name in ("show", "home", "travel", "cap", "delivery", "genre", "artist"):
        assert f"`/me {name}`" in text


def test_help_hides_reviewer_commands_from_members() -> None:
    """Listed-and-refused just invites a member to try something they cannot
    act on."""
    member_text = _profile_bot(None)._help_text(reviewer=False)
    reviewer_text = _profile_bot(None)._help_text(reviewer=True)
    assert "/event" not in member_text
    for name in ("submit", "approve", "reject", "set-role"):
        assert f"`/event {name}`" in reviewer_text


def test_help_omits_the_channel_guide_until_the_split_is_configured() -> None:
    plain = _profile_bot(None)._help_text(reviewer=False)
    assert "Channels" not in plain

    split = _profile_bot(
        None, regional_announcement_channel_id=9, local_radius_miles=60
    )._help_text(reviewer=False)
    assert "<#3>" in split and "<#9>" in split
    assert "60 miles" in split
    assert "Mute it" in split


def test_help_tells_reviewers_the_delivery_mode_is_not_live_yet() -> None:
    off = _profile_bot(None)._help_text(reviewer=True)
    assert "Per-user delivery is **off**" in off
    assert "not" in off.split("Per-user delivery")[1]

    live = _profile_bot(None, personal_delivery="on")._help_text(reviewer=True)
    assert "Per-user delivery is **on**" in live
    assert "profiles are recorded but not used yet" not in live


def test_help_explains_that_following_does_not_widen_matching() -> None:
    """The one thing members will otherwise get wrong about the feature."""
    text = _profile_bot(None)._help_text(reviewer=False)
    assert "does **not** widen" in text
    assert "catch-up" in text
