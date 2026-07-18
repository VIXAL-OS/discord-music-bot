from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from music_event_bot.config import Settings

logger = logging.getLogger(__name__)

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "title": {"type": "string"},
        "artist": {"type": "string"},
        "venue": {"type": "string"},
        "location": {"type": "string"},
        "starts_at": {"type": "string"},
        "url": {"type": "string"},
        "genres": {"type": "array", "items": {"type": "string"}},
    },
    "required": [
        "found",
        "title",
        "artist",
        "venue",
        "location",
        "starts_at",
        "url",
        "genres",
    ],
    "additionalProperties": False,
}


def _prompt(
    request_text: str,
    context: list[tuple[str, str]],
    now: datetime,
    page_text: str | None,
) -> str:
    context_lines = "\n".join(f"[{author}]: {text}" for author, text in context)
    page_block = (
        f"\n\nContent fetched from the linked page:\n{page_text}" if page_text else ""
    )
    return (
        "A Discord user asked a music-event bot to queue an event for review. "
        "Work out which real-world event they mean from their request, the "
        "recent channel conversation, and any linked page content.\n\n"
        f"Today is {now:%A, %B %d, %Y} ({now.tzinfo}). Dates without a year mean "
        "the next occurrence.\n\n"
        f"Recent channel messages (oldest first):\n{context_lines or '(none)'}\n\n"
        f"The request: {request_text or '(just a mention, no text)'}"
        f"{page_block}\n\n"
        "Rules:\n"
        "- found=true only when a concrete event (artist/show plus at least one "
        "of venue or date) is identifiable; otherwise found=false and leave "
        "every other field empty.\n"
        "- title: the show as a listing would name it (usually the headliner).\n"
        "- starts_at: ISO 8601 local time like 2026-07-18T20:00 ONLY when the "
        "conversation states both date and time; a date alone like 2026-07-18 "
        "when only the date is known; empty when unknown. NEVER invent a time.\n"
        "- location: street address only if stated; otherwise empty.\n"
        "- genres: a few niche genre labels for the act if you are confident, "
        "else an empty list.\n"
        "- Do not guess venues or dates that are not in the conversation."
    )


class RequestEventParser:
    """Infer event details for @mention requests from channel context."""

    def __init__(self, settings: Settings, client: Any | None = None) -> None:
        self.settings = settings
        self._client = client

    def _resolve_client(self) -> Any | None:
        if self._client is not None:
            return self._client
        api_key = self.settings.anthropic_api_key
        if api_key is None:
            return None
        from anthropic import AsyncAnthropic

        self._client = AsyncAnthropic(api_key=api_key.get_secret_value())
        return self._client

    async def extract(
        self,
        request_text: str,
        context: list[tuple[str, str]],
        now: datetime,
        page_text: str | None = None,
    ) -> dict[str, Any] | None:
        client = self._resolve_client()
        if client is None:
            return None
        import anthropic

        try:
            response = await client.messages.create(
                model=self.settings.genre_map_model,
                max_tokens=1000,
                system=(
                    "You extract concert details from Discord conversations for "
                    "an event bot. Be literal; never invent details the "
                    "conversation does not support."
                ),
                messages=[
                    {
                        "role": "user",
                        "content": _prompt(request_text, context, now, page_text),
                    }
                ],
                output_config={"format": {"type": "json_schema", "schema": _SCHEMA}},
            )
        except anthropic.APIError as exc:
            logger.warning("Event request extraction failed: %s", exc)
            return None
        if response.stop_reason in ("refusal", "max_tokens"):
            logger.warning(
                "Event request extraction stopped early (%s)", response.stop_reason
            )
            return None
        text = next(
            (block.text for block in response.content if block.type == "text"), None
        )
        if text is None:
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("Event request extraction returned invalid JSON")
            return None
        if not isinstance(payload, dict) or not payload.get("found"):
            return None
        if not str(payload.get("title", "")).strip():
            return None
        return payload
