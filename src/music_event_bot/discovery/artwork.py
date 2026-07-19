"""Recover event artwork from the listing pages that feeds link but never embed.

Calendar (ICS) and RSS listings carry no image of their own, so events from those
sources publish with a blank embed unless someone finds art by hand. Their
descriptions do link the venue's own event page, which almost always carries an
Open Graph image -- usually the exact show flyer. This module follows those links
and extracts it.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import urljoin, urlsplit

import httpx

from music_event_bot.discovery.images import is_publishable_image
from music_event_bot.domain.models import DiscoveredEvent

logger = logging.getLogger(__name__)

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_LINK_RE = re.compile(r"https?://[^\s<>\"')\]]+")

# Match the content attribute whichever side of the property it sits on, since
# hand-rolled and CMS-generated tags disagree about ordering.
_META_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE | re.DOTALL)
    for pattern in (
        r"<meta[^>]+(?:property|name)=[\"']og:image(?::url)?[\"'][^>]+content=[\"']([^\"']+)[\"']",
        r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+(?:property|name)=[\"']og:image(?::url)?[\"']",
        r"<meta[^>]+(?:property|name)=[\"']twitter:image[\"'][^>]+content=[\"']([^\"']+)[\"']",
    )
)

# The feed's own URL is the calendar itself, never a page about the show.
_USELESS_HOSTS = ("calendar.google.com", "google.com")

# Drusky pages exist for most Pittsburgh shows, but their art is either the house
# placeholder or an unrenderable .jfif, so they are tried only as a last resort --
# a venue's own page (preservingconcerts.com for Preserving Underground shows) wins
# whenever the listing includes one.
_DEPRIORITISED_HOSTS = ("druskyentertainment.com",)

_MAX_CANDIDATES = 3

# A listing index describes a season, not a show, so its og:image is the venue's
# header art. Feeds frequently link one of these instead of a per-show page.
_INDEX_PATHS = ("", "/", "/calendar", "/events", "/shows", "/schedule", "/tickets")


def _is_index_page(url: str) -> bool:
    path = urlsplit(url).path.rstrip("/").lower()
    for suffix in (".html", ".htm", ".php"):
        path = path.removesuffix(suffix)
    return path in {entry.rstrip("/") for entry in _INDEX_PATHS}


def candidate_pages(description: str | None, *urls: str | None) -> list[str]:
    """Listing pages worth checking, best first.

    Takes the raw description and any known page URLs rather than an event, so
    stored records (which carry ``url`` instead of ``source_url``) can reuse it.
    """
    seen: dict[str, None] = {}
    for raw in (*_LINK_RE.findall(description or ""), *(url or "" for url in urls)):
        url = raw.rstrip(".,);")
        if not url:
            continue
        host = urlsplit(url).netloc.lower()
        if not host or any(bad in host for bad in _USELESS_HOSTS):
            continue
        seen.setdefault(url, None)
    ranked = sorted(
        seen,
        key=lambda url: any(bad in urlsplit(url).netloc.lower() for bad in _DEPRIORITISED_HOSTS),
    )
    return ranked[:_MAX_CANDIDATES]


def _candidate_pages(event: DiscoveredEvent) -> list[str]:
    return candidate_pages(event.description, event.source_url)


def extract_og_image(html: str, page_url: str) -> str | None:
    for pattern in _META_PATTERNS:
        match = pattern.search(html)
        if match:
            return urljoin(page_url, str(match.group(1)).strip())
    return None


class ArtworkResolver:
    """Finds a publishable image for events whose source supplied none."""

    def __init__(self, client: httpx.AsyncClient | None = None, timeout: float = 10.0) -> None:
        self._client = client
        self._timeout = timeout
        # Venue pages repeat across a night's events; never fetch one twice per run.
        self._pages: dict[str, str | None] = {}
        self._verified: dict[str, bool] = {}
        self._site_defaults: dict[str, str | None] = {}
        self._claimed: dict[str, str] = {}
        self._shared: set[str] = set()

    @property
    def shared_images(self) -> frozenset[str]:
        """Images this run saw on more than one page, i.e. venue art.

        The first page to reach one is handed it before any duplicate proves it
        generic, so callers that can defer their writes should drop anything
        listed here before committing.
        """
        return frozenset(self._shared)

    async def resolve(self, event: DiscoveredEvent) -> str | None:
        return await self.resolve_pages(_candidate_pages(event))

    async def resolve_pages(self, pages: list[str]) -> str | None:
        if not pages:
            return None
        owned = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            headers={"User-Agent": _BROWSER_UA},
        )
        try:
            for page in pages:
                image = await self._image_for_page(client, page)
                if image:
                    return image
        finally:
            if owned:
                await client.aclose()
        return None

    async def _image_for_page(self, client: httpx.AsyncClient, page: str) -> str | None:
        if page in self._pages:
            image = self._pages[page]
        else:
            image = await self._scrape(client, page)
            self._pages[page] = image
        if not image or not await self._renders(client, image):
            return None
        # Some sites put one banner on every per-show page, and it survives both the
        # placeholder and site-default checks. Two different pages yielding the same
        # file proves it describes neither show, so refuse it from then on. The first
        # event to claim it keeps it -- that one lands in the daily image audit.
        owner = self._claimed.setdefault(image, page)
        if owner != page:
            self._shared.add(image)
            logger.info(
                "Artwork %s is shared by %s and %s; treating as venue art", image, owner, page
            )
            return None
        return image

    async def _scrape(self, client: httpx.AsyncClient, page: str) -> str | None:
        if _is_index_page(page):
            logger.debug("Skipping listing index %s: its art describes the venue", page)
            return None
        try:
            response = await client.get(page)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            logger.debug("Artwork lookup could not fetch %s: %s", page, exc)
            return None
        image = extract_og_image(response.text, str(response.url))
        if image and not is_publishable_image(image):
            logger.debug("Ignoring placeholder artwork %s from %s", image, page)
            return None
        if image and await self._is_site_default(client, page, image):
            logger.debug("Ignoring site-wide social image %s from %s", image, page)
            return None
        return image

    async def _is_site_default(self, client: httpx.AsyncClient, page: str, image: str) -> bool:
        """True when this is the site's stock social image rather than show art.

        Most small venue sites put one og:image on every page -- their logo, or a
        photo of the room -- and calendar feeds often link the calendar index rather
        than a per-show page. Both yield an image that passes every other check while
        depicting nothing about the act. Comparing against the site's front page
        separates real per-event art from the house default in one extra request.
        """
        parts = urlsplit(page)
        root = f"{parts.scheme}://{parts.netloc}/"
        if root not in self._site_defaults:
            default = None
            try:
                response = await client.get(root)
                response.raise_for_status()
                default = extract_og_image(response.text, str(response.url))
            except httpx.HTTPError as exc:
                # Fail open: an unreachable front page is no reason to drop real art.
                logger.debug("Could not read site default for %s: %s", root, exc)
            self._site_defaults[root] = default
        default = self._site_defaults[root]
        return default is not None and default.split("?", 1)[0] == image.split("?", 1)[0]

    async def _renders(self, client: httpx.AsyncClient, image: str) -> bool:
        """Confirm the asset is really an image.

        Some hosts serve flyers as application/octet-stream (Drusky's .jfif uploads
        do, alongside nosniff). Those return HTTP 200 but Discord renders nothing,
        producing a blank embed that no review step would catch.
        """
        if image in self._verified:
            return self._verified[image]
        ok = False
        try:
            response = await client.get(image, headers={"Range": "bytes=0-1023"})
            response.raise_for_status()
            ok = response.headers.get("content-type", "").lower().startswith("image/")
            if not ok:
                logger.debug(
                    "Rejecting %s: content-type %r",
                    image,
                    response.headers.get("content-type"),
                )
        except httpx.HTTPError as exc:
            logger.debug("Artwork candidate %s failed verification: %s", image, exc)
        self._verified[image] = ok
        return ok
