"""Tidy hand-typed venue listing titles.

Some venue calendars (Brillobox's is the case this was written for) put the
whole flyer into the event title:

    FRIDAY OCTOBER 9TH! 8PM DOORS! $20 ADV/$25 DOS CHOIR BOY/ CRASH DAVIDIAN/

Date, time and price already live in their own fields, and the shouting reads
badly on a review card and an announcement. This strips them out and keeps the
bill: "Choir Boy / Crash Davidian".

It is opt-in per feed (``MUSICBOT_FEED_TITLE_CLEANUP``), because elsewhere
capitals are deliberate -- arcane.city lists "IRON LUNG" because that is how
the band writes its name.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta

_DAYS = r"(?:MON|TUES?|WED(?:NES)?|THU(?:RS)?|FRI|SAT(?:UR)?|SUN)(?:DAY)?S?"
_MONTHS = (
    r"(?:JAN(?:UARY)?|FEB(?:RUARY)?|MAR(?:CH)?|APR(?:IL)?|MAY|JUNE?|JULY?|"
    r"AUG(?:UST)?|SEPT?(?:EMBER)?|OCT(?:OBER)?|NOV(?:EMBER)?|DEC(?:EMBER)?)"
)
_TIME = r"\d{1,2}(?::\d{2})?\s*(?:AM|PM)"

# Order matters: longer phrases go before the words they contain.
_NOISE = [
    rf"\bEVERY\s+{_DAYS}\b",
    rf"\b{_DAYS}(?:\s+NITE)?\b,?",
    rf"\b{_MONTHS}\.?\s+\d{{1,2}}(?:ST|ND|RD|TH)?\b,?",
    r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b",
    rf"\b(?:DOORS?|SHOW|MUSIC)\s*(?:@|AT)?\s*{_TIME}",
    rf"{_TIME}(?:\s*(?:-|–|TO)\s*{_TIME})?(?:\s*(?:DOORS?|SHOW))?",
    r"\$\s*\d+(?:\.\d{2})?(?:\s*(?:ADV|DOS|DOOR|AT THE DOOR|PRESALE))?",
    r"\bFREE TO PLAY\b",
    r"\b(?:ALWAYS\s+)?FREE\s+ON\s+THE\s+SECOND\s+FLOOR\b",
    r"\b(?:ON\s+THE\s+)?SECOND\s+FLOOR\b",
    r"\bALWAYS\s+FREE\b",
    r"\bFREE (?:SHOW|ENTRY|ADMISSION)\b",
    r"\bMORE INFO(?: (?:ON THE WAY|SOON|TBA|TO COME|COMING SOON|COMING))?\b",
    r"\bFOOD AND DRINK SPECIALS\b",
    r"\bDRINK SPECIALS\b",
    r"\bPRIZES\b",
    r"\bALL AGES\b",
    r"\b(?:18|21)\s*\+",
    r"\bPWYC\b",
    r"\bDOORS\b",
]
_NOISE_RE = re.compile("|".join(f"(?:{pattern})" for pattern in _NOISE), re.IGNORECASE)
_WITH_RE = re.compile(r"\bW/\s*", re.IGNORECASE)
_WITH_MARK = "\x00"
_EDGE_PUNCTUATION = " \t:;,.…-–—|*"
# Segments that carry no part of the bill once dates and prices are gone.
_EMPTY_SEGMENTS = frozenset({"free", "nite", "coming"})

# Kept in capitals by the title-caser.
_ACRONYMS = frozenset(
    {
        "DJ",
        "DJS",
        "MC",
        "TBA",
        "TBD",
        "EP",
        "LP",
        "PGH",
        "USA",
        "UK",
        "NYC",
        "LA",
        "BYOB",
        "RNB",
        "TV",
        "II",
        "III",
    }
)
_SMALL_WORDS = frozenset(
    {"a", "an", "and", "as", "at", "by", "for", "in", "of", "on", "or", "the", "to", "vs"}
)


def clean_listing_title(title: str) -> str:
    """Return the bill from a flyer-style title, or the title unchanged.

    Falls back to the original when nothing usable survives, so a title this
    does not understand is never blanked.
    """
    text = _WITH_RE.sub(f" {_WITH_MARK} ", title)
    text = _NOISE_RE.sub(" ", text)
    parts: list[str] = []
    for part in re.split(r"[!/]+", text):
        part = re.sub(r"\s+", " ", part).strip(_EDGE_PUNCTUATION)
        if part.casefold() in _EMPTY_SEGMENTS:
            continue
        # A segment that is only the "with" marker is a dangling "W/" etc.
        if (
            part
            and part.strip(f"{_WITH_MARK} ")
            and part.casefold() not in {seen.casefold() for seen in parts}
        ):
            parts.append(part)
    cleaned = " / ".join(parts)
    cleaned = re.sub(rf"\s*{_WITH_MARK}\s*", " w/ ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(_EDGE_PUNCTUATION + " ")
    cleaned = re.sub(r"\s+w/$", "", cleaned)
    if not cleaned:
        return title.strip()
    return _title_case(cleaned) if _is_shouting(cleaned) else cleaned


def correct_meridiem(starts_at: datetime | None, title: str) -> datetime | None:
    """Move a morning start to the evening when the title says PM.

    Hand-typed calendars sometimes store "7PM" as 07:00. Only the first time in
    the title counts, and only when it names the same hour as the stored start.
    """
    if starts_at is None:
        return None
    match = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*PM\b", title, re.IGNORECASE)
    if not match or starts_at.hour >= 12:
        return starts_at
    hour, minute = int(match.group(1)) % 12, int(match.group(2) or 0)
    if starts_at.hour == hour and starts_at.minute == minute:
        return starts_at + timedelta(hours=12)
    return starts_at


def _is_shouting(text: str) -> bool:
    letters = [character for character in text if character.isalpha()]
    return bool(letters) and sum(character.isupper() for character in letters) / len(letters) > 0.8


def _title_case(text: str) -> str:
    words = text.split(" ")
    result: list[str] = []
    for index, word in enumerate(words):
        previous = words[index - 1] if index else ""
        first = index == 0 or previous in {"/", "w/"} or previous.endswith(":")
        result.append(_case_word(word, first=first))
    return " ".join(result)


def _case_word(word: str, *, first: bool) -> str:
    if word in {"/", "w/"}:
        return word
    if re.fullmatch(r"\d+(?:st|nd|rd|th)", word, re.IGNORECASE):
        return word.casefold()
    plain = re.fullmatch(r"[A-Za-z']+", word) is not None
    if not first and plain and word.casefold() in _SMALL_WORDS:
        return word.casefold()
    # Case each run of letters on its own, so "GUHTS(NYC)" becomes "Guhts(NYC)"
    # and "EX-PILOTS" becomes "Ex-Pilots". Apostrophes and "$" stay inside a
    # run, so "TV’S" reads "TV’s" and a stylised "WOR$T" reads "Wor$t".
    return re.sub(r"(?:[^\W\d_]|['’$])+", _case_run, word)


def _case_run(match: re.Match[str]) -> str:
    run = match.group(0)
    stem = re.split(r"['’]", run, maxsplit=1)[0]
    if stem.upper() in _ACRONYMS:
        return stem.upper() + run[len(stem) :].casefold()
    return run[:1].upper() + run[1:].casefold()
