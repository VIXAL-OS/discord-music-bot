from __future__ import annotations

import json
from typing import Any

import pytest

from music_event_bot.config import Settings
from music_event_bot.taste.event_genres import EventGenreClassifier, genres_in_text

VOCAB = (
    "techno",
    "house",
    "goth",
    "darkwave",
    "post punk",
    "new wave",
    "metal",
    "death metal",
    "melodic death metal",
    "industrial",
    "avant garde",
    "other events",
)


def _classifier(client: Any | None = None) -> EventGenreClassifier:
    return EventGenreClassifier(Settings(_env_file=None), VOCAB, client=client)


def test_genres_named_in_prose_are_found() -> None:
    text = (
        "Non-Stop Erotic Cabaret returns for a night of post-punk, new wave, "
        "80s goth, and synth sleaze."
    )
    assert set(genres_in_text(text, VOCAB)) == {"post punk", "new wave", "goth"}


def test_ambiguous_single_words_do_not_fire_on_ordinary_prose() -> None:
    """'GOING TO DAD'S HOUSE' is a party name, not a genre claim."""
    assert genres_in_text("GOING TO DAD'S HOUSE W/ DJ DINI DADDY", VOCAB) == ()
    assert genres_in_text("Come rock out in our industrial district venue", VOCAB) == ()


def test_genre_words_inside_an_event_name_do_not_fire() -> None:
    """Event names borrow genre words constantly; the name is not a claim."""
    vocab = (*VOCAB, "club", "cabaret")
    assert genres_in_text("August Meeting of the Greensburg Vinyl Club", vocab) == ()
    assert "cabaret" not in genres_in_text("Non-Stop Erotic Cabaret: a goth night", vocab)


def test_the_most_specific_genre_wins() -> None:
    """A bill of melodic death metal is not also filed under plain metal."""
    assert genres_in_text("A night of melodic death metal", VOCAB) == ("melodic death metal",)


def test_text_without_genres_yields_nothing() -> None:
    assert genres_in_text("Join us for an evening of readings and snacks", VOCAB) == ()
    assert genres_in_text("", VOCAB) == ()


def test_from_text_reads_title_and_description_together() -> None:
    found = _classifier().from_text("Cycle Techno", "A night of driving music.")
    assert found == ("techno",)


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.stop_reason = "end_turn"
        self.content = [type("Block", (), {"type": "text", "text": json.dumps(payload)})()]


class _FakeMessages:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> _FakeResponse:
        self.calls.append(kwargs)
        return _FakeResponse(self.payload)


class _FakeClient:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.messages = _FakeMessages(payload)


@pytest.mark.asyncio
async def test_classify_returns_vocabulary_genres() -> None:
    client = _FakeClient({"events": [{"id": "e1", "genres": ["Techno", "house"]}]})
    result = await _classifier(client).classify(
        [{"id": "e1", "title": "Cycle Techno", "venue": "Bantha", "description": "dj night"}]
    )
    assert result == {"e1": ("techno", "house")}


@pytest.mark.asyncio
async def test_genres_outside_the_vocabulary_are_dropped() -> None:
    """A label with no genre_roles row routes nowhere, so it is not worth storing."""
    client = _FakeClient({"events": [{"id": "e1", "genres": ["sea shanty", "goth"]}]})
    result = await _classifier(client).classify(
        [{"id": "e1", "title": "Some Night", "venue": "", "description": ""}]
    )
    assert result == {"e1": ("goth",)}


@pytest.mark.asyncio
async def test_a_blank_answer_assigns_nothing() -> None:
    """Music with no discernible genre stays blank and reaches the music catch-all."""
    client = _FakeClient({"events": [{"id": "e1", "genres": []}]})
    result = await _classifier(client).classify(
        [{"id": "e1", "title": "Vinyl Club", "venue": "Gallery", "description": "a meeting"}]
    )
    assert result == {}


@pytest.mark.asyncio
async def test_non_music_events_are_labelled_rather_than_left_blank() -> None:
    """A blank would fall through to the music catch-all and ping the wrong room.

    "Other Events" only receives listings that arrive carrying its own label, so
    the classifier has to say so outright.
    """
    client = _FakeClient({"events": [{"id": "e1", "genres": ["other events"]}]})
    result = await _classifier(client).classify(
        [{"id": "e1", "title": "Mushroom Walk", "venue": "Frick", "description": "a walk"}]
    )
    assert result == {"e1": ("other events",)}


def test_the_prompt_asks_for_the_non_music_label() -> None:
    from music_event_bot.taste.event_genres import _prompt

    prompt = _prompt(
        [{"id": "e1", "title": "t", "venue": "v", "description": "d"}], VOCAB
    )
    assert "other events" in prompt


@pytest.mark.asyncio
async def test_answers_for_unrequested_events_are_ignored() -> None:
    client = _FakeClient({"events": [{"id": "other", "genres": ["goth"]}]})
    result = await _classifier(client).classify(
        [{"id": "e1", "title": "x", "venue": "", "description": ""}]
    )
    assert result == {}


@pytest.mark.asyncio
async def test_classify_is_skipped_without_an_api_key() -> None:
    classifier = EventGenreClassifier(Settings(_env_file=None, anthropic_api_key=None), VOCAB)
    entries = [{"id": "e1", "title": "x", "venue": "", "description": ""}]
    assert await classifier.classify(entries) == {}
