from __future__ import annotations

import asyncio
import logging
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from music_event_bot.domain.blocklist import Blocklist
from music_event_bot.domain.geography import GeoPoint, haversine_miles
from music_event_bot.domain.models import EventRecord, EventStatus
from music_event_bot.services.delivery import (
    UserMatch,
    build_profiles,
    match_users,
    roles_for_event,
    spends_budget,
)
from music_event_bot.storage.repositories import EventRepository

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AnnouncementRouting:
    """Where an announcement goes and who it pings."""

    channel_id: int | None
    role_ids: tuple[int, ...]
    is_regional: bool
    # Per-user delivery only. Empty while personal_delivery is off, and
    # legitimately empty when an event matches nobody -- that posts silently
    # rather than falling back to the catch-all role, so the channel stays a
    # complete listing without pinging a community that did not ask for it.
    user_ids: tuple[int, ...] = ()


class PublicationGateway(Protocol):
    async def create_or_find_scheduled_event(self, event: EventRecord) -> int | None: ...

    async def create_or_find_announcement(
        self,
        event: EventRecord,
        role_ids: tuple[int, ...],
        scheduled_event_id: int | None,
        channel_id: int | None = None,
        user_ids: tuple[int, ...] = (),
    ) -> int: ...

    async def update_published_event(
        self,
        event: EventRecord,
        scheduled_event_id: int | None,
        announcement_message_id: int,
        role_ids: tuple[int, ...],
        channel_id: int | None = None,
        user_ids: tuple[int, ...] = (),
    ) -> None: ...


class PublicationService:
    def __init__(
        self,
        repository: EventRepository,
        gateway: PublicationGateway,
        fallback_role_ids: frozenset[int] = frozenset(),
        catchall_role_ids: frozenset[int] | None = None,
        blocklist: Blocklist | None = None,
        announcement_channel_id: int | None = None,
        regional_announcement_channel_id: int | None = None,
        home: GeoPoint | None = None,
        local_radius_miles: int = 0,
        personal_delivery: str = "off",
        bucket_roles: dict[str, int] | None = None,
        timezone: ZoneInfo | None = None,
        reserved_ping_slots: int = 0,
    ) -> None:
        self.repository = repository
        self.gateway = gateway
        # Shows beyond local_radius_miles are announced in their own channel
        # with no role ping. Role mentions are a union, never an
        # intersection: a member who wants goth shows in town but not four
        # states away cannot express that by holding or dropping roles, so
        # the split has to happen before Discord sees the message. With no
        # regional channel configured, everything routes as it always has.
        self.announcement_channel_id = announcement_channel_id
        self.regional_channel_id = regional_announcement_channel_id
        self.home = home
        self.local_radius_miles = local_radius_miles
        # Shadow mode: work out the per-user mention list and log it without
        # sending it, so the flip can be measured against a week of real
        # announcements before anyone's notifications change.
        self.personal_delivery = personal_delivery
        self.bucket_roles = bucket_roles or {}
        # Daily ping budgets are counted against local midnight, not a
        # rolling window, so "five a day" means what a member would assume.
        self.timezone = timezone or ZoneInfo("UTC")
        # The tail of each member's daily budget, spendable only by a show
        # they would be annoyed to miss. Without it the cap rations by
        # publish order, which on a busy day means the first five rather
        # than the best five.
        self.reserved_ping_slots = reserved_ping_slots
        # Discovery filters the blocklist at ingest, which does nothing for an
        # event that was already approved when the act was added to the roster.
        # This is the last gate before anything reaches the community.
        self.blocklist = blocklist or Blocklist()
        # Catch-all buckets ("Other Music", "Other Events") only ping when no
        # specific role matched; otherwise every event tagged "rock" would
        # also ping the catch-all community.
        self.fallback_role_ids = fallback_role_ids
        # When an event matches nothing anywhere, only these roles hear about
        # it (typically just "Other Music", not every catch-all bucket).
        self.catchall_role_ids = (
            catchall_role_ids if catchall_role_ids is not None else fallback_role_ids
        )
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def _roles_for(self, event: EventRecord) -> tuple[int, ...]:
        return await roles_for_event(
            self.repository,
            event,
            fallback_role_ids=self.fallback_role_ids,
            catchall_role_ids=self.catchall_role_ids,
        )

    async def _personal_matches(self, event: EventRecord) -> tuple[UserMatch, ...]:
        """Members whose taste and travel range cover this event, capped.

        Unlike the replay report, the cap here has to be applied against what
        has already gone out today, which lives in the notification ledger.
        """
        rows = await self.repository.list_user_profiles()
        if not rows:
            return ()
        profiles = build_profiles(
            rows,
            await self.repository.list_user_taste("genre"),
            self.bucket_roles,
            await self.repository.list_user_taste_weighted("artist"),
            await self.repository.list_user_taste_weighted("venue"),
        )
        matches = match_users(event, await self._roles_for(event), profiles)
        midnight = (
            datetime.now(UTC)
            .astimezone(self.timezone)
            .replace(hour=0, minute=0, second=0, microsecond=0)
        )
        decided: list[UserMatch] = []
        for match in matches:
            if not match.within_band:
                # Out of range is not overflow: it never reaches the catch-up
                # post either, so it is neither pinged nor queued.
                decided.append(replace(match, pinged=False))
                continue
            budget = profiles[match.user_id].daily_ping_cap
            spent = await self.repository.count_pings_since(match.user_id, midnight)
            decided.append(
                replace(
                    match,
                    pinged=spends_budget(match.score, spent, budget, self.reserved_ping_slots),
                )
            )
        return tuple(decided)

    async def _personal_routing(
        self, event: EventRecord, routing: AnnouncementRouting
    ) -> AnnouncementRouting:
        """Fold per-user delivery into a routing decision.

        Under "on" the role mentions come off and the matched members go on.
        Two guards: with no profiles seeded at all, role pings stay, because
        the alternative is the whole server going quiet on a config typo.
        An event that simply matches nobody is different and posts silently.
        """
        if self.personal_delivery == "off":
            return routing
        matches = await self._personal_matches(event)
        if not matches and not await self.repository.list_user_profiles():
            logger.warning(
                "personal_delivery is %r but no profiles are seeded; keeping role pings. "
                "Run seed-profiles --apply.",
                self.personal_delivery,
            )
            return routing
        pinged = tuple(match.user_id for match in matches if match.pinged)
        queued = tuple(match.user_id for match in matches if match.within_band and not match.pinged)
        if self.personal_delivery == "shadow":
            names = {match.user_id: match.display_name for match in matches}
            logger.info(
                "Shadow delivery for %r: would mention %d (%s), queue %d, "
                "%d out of range",
                event.title,
                len(pinged),
                ", ".join(names[user_id] for user_id in pinged) or "nobody",
                len(queued),
                sum(1 for match in matches if not match.within_band),
            )
            return routing
        await self.repository.record_notifications(
            event.id,
            {
                **{user_id: "queued" for user_id in queued},
                **{user_id: "pinged" for user_id in pinged},
            },
        )
        if queued:
            logger.info(
                "Queued %r for %d member(s) past their daily cap", event.title, len(queued)
            )
        return replace(routing, role_ids=(), user_ids=pinged)

    def _is_regional(self, event: EventRecord) -> bool:
        if self.regional_channel_id is None or self.home is None or self.local_radius_miles <= 0:
            return False
        if event.venue_latitude is None or event.venue_longitude is None:
            # An address the book does not cover is far likelier to be a DIY
            # room in town than a show four states away, and burying a local
            # show is a worse failure than announcing a distant one.
            return False
        distance = haversine_miles(
            self.home, GeoPoint(event.venue_latitude, event.venue_longitude)
        )
        return distance > self.local_radius_miles

    async def _routing_for(
        self, event: EventRecord, *, channel_id: int | None = None
    ) -> AnnouncementRouting:
        """Pick the channel an announcement goes to and the roles it pings.

        Passing channel_id pins the decision to the channel a card already
        lives in. A venue correction can move an event across the local
        boundary weeks after publication, but the message cannot move with
        it, and a card posted silently must not gain a role ping on edit.
        """
        if channel_id is not None and self.regional_channel_id is not None:
            regional = channel_id == self.regional_channel_id
        else:
            regional = self._is_regional(event)
        if regional:
            return AnnouncementRouting(
                channel_id if channel_id is not None else self.regional_channel_id, (), True
            )
        return AnnouncementRouting(
            channel_id if channel_id is not None else self.announcement_channel_id,
            await self._roles_for(event),
            False,
        )

    async def publish(self, event_id: str) -> EventRecord:
        async with self._locks[event_id]:
            event = await self.repository.get_event(event_id)
            if event is None:
                raise KeyError(f"Unknown event ID: {event_id}")
            publication = await self.repository.begin_publication(event_id)
            try:
                # Inside the try on purpose: the existing handler records this
                # as publish_failed, so the reason surfaces on the review card
                # for a human to reject. Dropping it silently would read as a
                # bug, and Retry deliberately keeps failing until someone does.
                blocked = self.blocklist.match(event)
                if blocked is not None:
                    raise ValueError(
                        f"{blocked.name} is on the artist blocklist "
                        f"(matched on {blocked.matched_on}): {blocked.reason}"
                    )
                scheduled_id = publication.get("scheduled_event_id")
                if scheduled_id:
                    scheduled_event_id = int(scheduled_id)
                else:
                    # None when the guild is at Discord's scheduled-event cap:
                    # the announcement still goes out, just without the
                    # native event link.
                    scheduled_event_id = await self.gateway.create_or_find_scheduled_event(event)
                    if scheduled_event_id is not None:
                        await self.repository.record_scheduled_event(event_id, scheduled_event_id)

                announcement_id = publication.get("announcement_message_id")
                if announcement_id:
                    announcement_message_id = int(announcement_id)
                else:
                    routing = await self._personal_routing(
                        event, await self._routing_for(event)
                    )
                    if routing.is_regional:
                        logger.info(
                            "Announcing %r in the regional channel without role pings",
                            event.title,
                        )
                    announcement_message_id = await self.gateway.create_or_find_announcement(
                        event,
                        routing.role_ids,
                        scheduled_event_id,
                        routing.channel_id,
                        routing.user_ids,
                    )
                    await self.repository.record_announcement(
                        event_id, announcement_message_id, routing.channel_id
                    )

                await self.repository.mark_published(event_id)
            except Exception as exc:
                await self.repository.mark_publish_failed(event_id, str(exc))
                raise

            published = await self.repository.get_event(event_id)
            if published is None:
                raise RuntimeError("Published event disappeared")
            return published

    async def drain_approved(
        self,
        *,
        limit_per_hour: int,
        start_hour: int,
        end_hour: int,
        timezone: ZoneInfo,
        now: datetime | None = None,
    ) -> int:
        """Publish queued approved events within the hourly ping budget.

        Runs only between start_hour and end_hour local time so nobody is
        pinged overnight. The budget counts everything published in the last
        hour (from any trigger), and the queue drains soonest show first.
        Returns the number of events published this call.
        """
        if limit_per_hour <= 0:
            return 0
        current = (now or datetime.now(UTC)).astimezone(timezone)
        if not start_hour <= current.hour < end_hour:
            return 0
        recent = await self.repository.count_publications_since(current - timedelta(hours=1))
        budget = limit_per_hour - recent
        published = 0
        for event in await self.repository.list_events(EventStatus.APPROVED):
            if published >= budget:
                break
            try:
                await self.publish(event.id)
                published += 1
            except Exception:
                # publish() already marked the event publish_failed; its
                # review card regains a Retry button on the next sync.
                logger.exception("Queued publication failed for event %s", event.id)
        return published

    async def update_existing(self, event_id: str) -> None:
        async with self._locks[event_id]:
            event = await self.repository.get_event(event_id)
            publication = await self.repository.get_publication(event_id)
            if event is None or publication is None:
                return
            scheduled_id = publication.get("scheduled_event_id")
            announcement_id = publication.get("announcement_message_id")
            if not announcement_id:
                return
            stored_channel = publication.get("announcement_channel_id")
            # Publications recorded before the local/regional split carry no
            # channel, and every one of those cards is in the main channel.
            # Re-routing their edits by distance would send an artwork
            # backfill to the regional channel to edit a message that is not
            # there.
            pinned = int(stored_channel) if stored_channel else self.announcement_channel_id
            routing = await self._routing_for(event, channel_id=pinned)
            if self.personal_delivery == "on":
                # The card names who was actually pinged, read back from the
                # ledger rather than recomputed. An artwork backfill weeks
                # later must not rewrite history because someone has since
                # changed their profile.
                routing = replace(
                    routing,
                    role_ids=(),
                    user_ids=await self.repository.get_pinged_users(event_id),
                )
            await self.gateway.update_published_event(
                event,
                int(scheduled_id) if scheduled_id else None,
                int(announcement_id),
                routing.role_ids,
                routing.channel_id,
                routing.user_ids,
            )
