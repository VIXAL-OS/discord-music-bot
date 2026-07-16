from music_event_bot.config import Settings
from music_event_bot.domain.models import TasteProfile


def profile_from_settings(settings: Settings) -> TasteProfile:
    return TasteProfile(
        artists=settings.artists,
        genres=settings.genres,
        venues=settings.venues,
    )
