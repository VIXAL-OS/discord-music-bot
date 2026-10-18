from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from music_event_bot.discovery.listing_titles import clean_listing_title, correct_meridiem

_EASTERN = ZoneInfo("America/New_York")


# Real titles from Brillobox's Event Organiser feed, October 2026.
@pytest.mark.parametrize(
    ("raw", "cleaned"),
    [
        (
            "FRIDAY OCTOBER 9TH! 8PM DOORS! $20 ADV/$25 DOS CHOIR BOY/ CRASH DAVIDIAN/"
            " DJ KRASS ADVERT/",
            "Choir Boy / Crash Davidian / DJ Krass Advert",
        ),
        (
            "FRIDAY OCTOBER 10TH 9PM/ $15 RED RABBIT/ ODNO OKNO/ WEIRD ERA/ PURPLE BERRIES",
            "Red Rabbit / Odno Okno / Weird Era / Purple Berries",
        ),
        (
            "OCTOBER 11TH 7PM DRUSKY PRESENTS: THE KNOW HARD FEELINGS TOUR W/ AITA/"
            " AUSTINETTE/ ALTARBOY",
            "Drusky Presents: The Know Hard Feelings Tour w/ Aita / Austinette / Altarboy",
        ),
        ("OCTOBER 30TH SECOND SKIN 9PM/$10", "Second Skin"),
        (
            "OCTOBER 31ST 8PM DECAYDZ (SPECIAL HALLOWEEN COVER SHOW) MORE INFO SOON",
            "Decaydz (Special Halloween Cover Show)",
        ),
        (
            "SATURDAY OCTOBER 24TH 8PM PHLOC RECORD RELEASE SHOW! MORE INFO ON THE WAY!",
            "Phloc Record Release Show",
        ),
        ("OCTOBER 23RD 10PM/$10 DJ GUN RAY PRESENTS:", "DJ Gun Ray Presents"),
        (
            "EVERY WEDNESDAY!!! BRILLO PUB QUIZ!!! FREE TO PLAY!!! PRIZES!!!"
            "FOOD AND DRINK SPECIALS!!!",
            "Brillo Pub Quiz",
        ),
        (
            "OCTOBER 1ST 9PM/ FREE ON THE SECOND FLOOR! PITTSBURGH IMPROV JAM!!!",
            "Pittsburgh Improv Jam",
        ),
        (
            "AUGUST 14TH TV’S NORM VIDEO RELEASE PARTY WITH DEATH INSTINCT AND GINA GORY!!!",
            "TV’s Norm Video Release Party With Death Instinct and Gina Gory",
        ),
        (
            "SUNDAY JUNE 21ST!!! 6PM/$15!!! A SPECIAL EARLY SHOW! GUHTS(NYC)/SARLACC!!!!",
            "A Special Early Show / Guhts(NYC) / Sarlacc",
        ),
        (
            "SEPTEMBER 12TH 10PM/$10!!! DJ RAY GUN PRESENTS: WOR$T GIRL$ IN AMERICA!!",
            "DJ Ray Gun Presents: Wor$t Girl$ in America",
        ),
        ("SEPTEMBER 26TH MELT!!!! MORE INFO COMING!!!", "Melt"),
    ],
)
def test_flyer_titles_keep_only_the_bill(raw: str, cleaned: str) -> None:
    assert clean_listing_title(raw) == cleaned


def test_titles_already_in_mixed_case_keep_their_casing() -> None:
    assert clean_listing_title("Oct 30 Second Skin w/ DJ Erica Scary") == (
        "Second Skin w/ DJ Erica Scary"
    )


def test_title_with_nothing_left_falls_back_to_the_original() -> None:
    assert clean_listing_title("OCTOBER 30TH 9PM/$10") == "OCTOBER 30TH 9PM/$10"


def test_morning_start_moves_to_the_evening_the_title_names() -> None:
    stored = datetime(2026, 10, 11, 7, 0, tzinfo=_EASTERN)
    title = "OCTOBER 11TH 7PM DRUSKY PRESENTS: THE KNOW HARD FEELINGS TOUR"
    assert correct_meridiem(stored, title) == datetime(2026, 10, 11, 19, 0, tzinfo=_EASTERN)


@pytest.mark.parametrize(
    ("hour", "title"),
    [
        (20, "FRIDAY OCTOBER 9TH! 8PM DOORS!"),  # already evening
        (7, "OCTOBER 11TH 9PM SHOW"),  # different hour: not a typo we can prove
        (7, "SUNDAY BRUNCH"),  # no time in the title
    ],
)
def test_start_is_left_alone_unless_the_title_proves_a_typo(hour: int, title: str) -> None:
    stored = datetime(2026, 10, 11, hour, 0, tzinfo=_EASTERN)
    assert correct_meridiem(stored, title) == stored
