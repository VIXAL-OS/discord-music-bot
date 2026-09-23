# Music Event Bot

A functional Python/Discord MVP that discovers upcoming music events, scores them against a community taste profile, sends candidates to a private review channel, and publishes approved listings as:

1. Native **Discord Scheduled Events** that members can subscribe to.
2. Public announcement embeds with an optional genre-role mention.

The original generated prototype is preserved unchanged at `legacy/music_event_scraper_prototype.py`. The root `music-event-scraper.py` is now a compatibility launcher for the new package.

## Architecture

```text
discovery sources ─┐
  Ticketmaster     │
  ICS / webcal     ├─> normalize ─> score ─> SQLite ─> review queue ─> Discord
  RSS / Atom       │                          (events, provenance,     (human
  Squarespace      │                           reviews, publications)   approval)
  arcane.city      │                                                       │
  manual submit  ──┘                                              scheduled event
                                                                + announcement embed
```

Each layer is a separate package under `src/music_event_bot/`: `discovery/` (one
adapter per source, all returning the same `DiscoveredEvent`), `domain/` (pure
scoring, normalization, geography, blocklist — no I/O), `storage/` (schema,
migrations, repositories), `services/` (orchestration, dedupe, publishing),
and `discord/` (bot, review UI, publication gateway). The domain layer has no
database or network imports, which is what makes the scoring rules directly
testable.

## Data layer

Storage is SQLite through [`aiosqlite`](https://github.com/omnilib/aiosqlite),
with the schema in [`storage/migrations.py`](src/music_event_bot/storage/migrations.py),
connection handling in [`storage/database.py`](src/music_event_bot/storage/database.py),
and queries in [`storage/repositories.py`](src/music_event_bot/storage/repositories.py).

**Schema.** Eleven application tables plus `schema_migrations`. Foreign keys are
enforced (`PRAGMA foreign_keys = ON`) and every child table — `event_sources`,
`reviews`, `publications`, `rsvps` — cascades from `events` on delete. Integrity
is pushed into the schema rather than the application: a `UNIQUE` fingerprint on
`events`, `UNIQUE(source_name, source_event_id)` on `event_sources` so one source
cannot file the same listing twice, composite primary keys on the tag and
preference tables, and a `CHECK` constraint restricting `rsvps.state` to
`going` / `interested` / `declined`.

**Migrations.** Seven forward-only migrations, applied in order and recorded in
`schema_migrations`. Each runs inside `BEGIN IMMEDIATE` and rolls back as a unit
on failure, so a partial upgrade cannot leave a half-built schema behind.
Startup is idempotent — already-applied versions are skipped — and a database
whose version is *newer* than the running code is refused outright rather than
silently downgraded. `WAL` journalling and a five-second `busy_timeout` let the
scheduler's discovery job write while the Discord bot reads.

**Idempotent ingestion.** The same show routinely arrives from several sources
under different titles. Events are keyed by a derived fingerprint
(`title | venue | start-minute`); ingest upserts on that key with
`INSERT ... ON CONFLICT ... DO UPDATE`, attaching each contributing source as a
row in `event_sources` rather than creating a second event. Because all three
fingerprint inputs can be corrected after first sight — a venue often arrives
late — the key is re-synced on write, and `music-event-bot dedupe` repairs rows
written before that behaviour existed. There are eleven upserts across the
repository layer, split between `DO UPDATE` and `DO NOTHING` depending on
whether later sources should overwrite earlier ones.

**Query patterns.** Reads join `events` against `reviews`, `publications`, and
`event_sources` (both inner and `LEFT JOIN`, the latter to find events that have
*no* review row yet). Indexes are built for the queries that actually run: the
review queue is served by a covering composite index on
`(status, score DESC, starts_at, id)` that matches its exact sort order, with
five more supporting status/date lookups, fingerprint matching, provenance
lookups, and the most-recent-run-per-job query.

**Tests.** 221 tests run against real SQLite databases in `tmp_path` — no mocks
at the storage boundary. Beyond round-tripping, they cover the cases that
actually break databases: migrations applied twice, a *populated* v1 database
migrated forward to v2 with its rows intact, an upsert merging a second source
into an existing event, and review-queue ordering under ties.

## What works in the MVP

- Manual artist, genre, and venue preferences.
- Optional Spotify import from one community curator's account: top artists, top
  tracks (heavy-rotation artists), and every artist with a configurable number of
  Liked Songs (default 12 — roughly an album's worth).
- Optional Last.fm community tags per artist for niche genre coverage, cached in
  SQLite.
- Optional Claude-powered mapping of niche tags onto the community's Discord
  genre buckets and broad Ticketmaster-style genres. Mappings are cached and
  reused deterministically; only never-seen tags are sent to the model.
- Ticketmaster Discovery API searches by location, radius, date window, and music classification.
- ICS/webcal calendars, including recurring events.
- RSS/Atom feeds with explicitly structured event fields; incomplete entries are held for editing rather than guessed.
- Manual Discord event submission.
- SQLite migrations, source provenance, idempotent ingestion, and cross-source fingerprints.
- Private Discord review cards with persistent Approve, Edit, Reject, and Retry controls.
- Reviewer authorization by user ID and/or Discord role ID.
- Native external Discord Scheduled Events.
- Public role-tagged announcements with constrained allowed mentions.
- Scheduler jobs, restart reconciliation, health output, and one-shot discovery commands.
- Docker deployment with a persistent SQLite volume.

The core is deliberately deterministic and does **not** require an AI API. Event dates, venues, and publication decisions are never invented by a model. The one optional AI feature — genre tag mapping — runs at taste-import time, is cached in SQLite, and never touches event data or publication decisions.

## Requirements

- Python 3.11+
- A Discord application/bot for live review and publication
- A Ticketmaster API key if Ticketmaster discovery is enabled
- Optional Spotify developer credentials for taste import

## Installation

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
Copy-Item .env.example .env
```

Or with Docker:

```powershell
Copy-Item .env.example .env
# Fill in .env first
docker compose up --build -d
```

Do not commit `.env`. It is excluded by `.gitignore`.

## Configuration

Every setting uses the `MUSICBOT_` prefix. See `.env.example` for the full list.

### Required for the Discord bot

| Variable | Purpose |
|---|---|
| `MUSICBOT_DISCORD_TOKEN` | Discord bot token |
| `MUSICBOT_DISCORD_GUILD_ID` | Target server ID |
| `MUSICBOT_REVIEW_CHANNEL_ID` | Private moderator/reviewer channel |
| `MUSICBOT_ANNOUNCEMENT_CHANNEL_ID` | Public event announcement channel |
| `MUSICBOT_REGIONAL_ANNOUNCEMENT_CHANNEL_ID` | Optional second channel for shows beyond `MUSICBOT_LOCAL_RADIUS_MILES`, posted there with no role ping. Blank = one channel for everything |
| `MUSICBOT_LOCAL_RADIUS_MILES` | Local/regional boundary in miles (default 75). Venues with no coordinates count as local |
| `MUSICBOT_PERSONAL_DELIVERY` | Per-user delivery rollout: `off` (role pings), `shadow` (log what per-user mentions would send), `on` (mentions replace role pings). Default `off` |
| `MUSICBOT_DEFAULT_DAILY_PING_CAP` | Ping budget a seeded profile starts with (default 5); overflow queues for the daily catch-up post |
| `MUSICBOT_RESERVED_PING_SLOTS` | Tail of each daily cap reserved for shows featuring an act the member follows (default 2); 0 disables it |
| `MUSICBOT_CATCHUP_HOUR` | Local hour for the daily catch-up post that drains queued overflow (default 18) |
| `MUSICBOT_CATCHUP_MAX_EVENTS` | Most events one catch-up post lists before it says "and N more" (default 15) |
| `MUSICBOT_ADMIN_USER_IDS` | Comma-separated authorized user IDs |
| `MUSICBOT_REVIEWER_ROLE_IDS` | Comma-separated authorized role IDs; either this or admin IDs is required |
| `MUSICBOT_GENRE_ROLE_MAP` | JSON object mapping normalized genres to role IDs |

Example:

```dotenv
MUSICBOT_ADMIN_USER_IDS=111111111111111111
MUSICBOT_REVIEWER_ROLE_IDS=222222222222222222
MUSICBOT_GENRE_ROLE_MAP={"indie rock":"333333333333333333","electronic":"444444444444444444"}
```

Enable Discord Developer Mode and use **Copy ID** to obtain guild, channel, user, and role IDs.

### Discord permissions

Install the bot in the target server with permission to:

- View the private review and public announcement channels
- Send messages and embed links
- Read message history
- Use application commands
- Create and manage events
- Mention the configured genre roles

The configured review channel should be private at the Discord channel-permission level. The bot also checks reviewer authorization inside every command, button, and modal callback.

The bot does not need the privileged Message Content Intent because the MVP uses slash commands and interactions instead of `!` commands.

### Ticketmaster

A regular individual can create a free [Ticketmaster Developer account](https://developer.ticketmaster.com/products-and-docs/apis/getting-started/) and use the default application's **Consumer Key** with the public Discovery API. You do not need to be a musician, venue, or Ticketmaster partner. Ticket purchasing/partner APIs are separate restricted products.

Put the Consumer Key only in your private `.env`:

```dotenv
MUSICBOT_TICKETMASTER_API_KEY=...
MUSICBOT_HOME_LATITUDE=40.4406
MUSICBOT_HOME_LONGITUDE=-79.9959
MUSICBOT_MAX_TRAVEL_RADIUS_MILES=350
MUSICBOT_TICKETMASTER_CELL_RADIUS_MILES=90
MUSICBOT_TICKETMASTER_MAX_PAGES=2
MUSICBOT_DISCOVERY_START_OFFSET_DAYS=1
MUSICBOT_DISCOVERY_HORIZON_DAYS=180
```

Set the home coordinates to the centre of your own region; the example above is downtown Pittsburgh. At these defaults the bot generates 31 overlapping Ticketmaster coverage cells across the complete 350-mile travel region. This includes Pittsburgh, intermediate cities such as Youngstown, Morgantown, Harrisburg, Erie, and Buffalo, and outer destinations such as Cleveland, Columbus, Philadelphia, and Toronto. Generated cells do not apply a US-only country filter, allowing Canadian events to appear. Coverage settings that would exceed the 64-cell safety bound are rejected with an instruction to increase the cell radius.

Each cell has its own small pagination budget so dense Pittsburgh inventory cannot crowd distant markets out of one giant date-sorted result set. At the defaults, a run makes at most 62 Ticketmaster page requests and normally fewer. Overlapping results are deduplicated by Ticketmaster event ID.

Ticketmaster is disabled cleanly when the Consumer Key is absent. The older single-coordinate settings remain supported as a compatibility mode.

### Taste profile

Comma-separated values:

```dotenv
MUSICBOT_PREFERRED_ARTISTS=Little Simz,Godspeed You! Black Emperor
MUSICBOT_PREFERRED_GENRES=hip hop,post rock,ambient
MUSICBOT_PREFERRED_VENUES=Mr. Smalls Theatre,Stage AE
MUSICBOT_MINIMUM_AFFINITY_SCORE=15
MUSICBOT_MINIMUM_MATCH_SCORE=0
```

Scoring is deterministic:

- Artist match: 60 affinity points
- Genre matches: up to 30 affinity points
- Related (weak) genre matches: up to 10 affinity points
- Venue match: 10 affinity points (corroboration only — a pinned venue cannot pass the review gate without a matching artist or genre)
- Distance preference: up to 20 additional points, decaying nonlinearly from the configured home coordinates to zero at the travel radius
- Total is capped at 100

Genres carry two evidence tiers. Niche tags and manually configured genres are **strong** evidence (15 points per match): a single match can put an event into review. Umbrella genres (`MUSICBOT_UMBRELLA_GENRES` — "rock", "pop", "country", …) and all genres produced by upward mapping are **weak** evidence (5 points per match, capped at 10): they corroborate events that already have real evidence but can never pass the 15-point review gate alone. Without this split, a taste profile enriched with hundreds of tags would match essentially every event Ticketmaster returns.

Ticketmaster discovery requires at least 15 music-affinity points before distance is added. Therefore an unrelated local listing does not enter review merely because it is nearby, while a favorite artist in Toronto or Philadelphia still qualifies easily. Curated ICS/RSS feeds and manual Discord submissions are not subject to the Ticketmaster affinity gate. Events without coordinates receive neither a distance bonus nor a penalty. The private review queue is ordered by total score and then event date.

### ICS and RSS

```dotenv
MUSICBOT_ICS_URLS=https://venue.example/events.ics,webcal://calendar.example/shows.ics
MUSICBOT_RSS_URLS=https://venue.example/events.xml
```

ICS is the preferred feed format because it carries structured dates, locations, timezones, and stable UIDs.

One of the configured feeds is the bot's **own** output calendar ("Pittsburgh Heavy Events"), which is deliberate — it is how out-of-band curator jobs hand events to the bot — but it means a show can reach the bot twice: once from a scraper, once as a calendar entry written under a different title. Those tasks share one title convention and check `events --venue ... --on ...` before writing for exactly this reason. See **Duplicate events** under Commands for the clean-up path.

`MUSICBOT_ICS_URLS` entries may also be **local file paths** (e.g. `data/email-events.ics`). A configured file that does not exist yet is skipped with a warning, so an out-of-band curator — a person, or a scheduled job that parses venue emails — can hand events to the bot by maintaining a calendar file. Feed fetchers send a browser-style User-Agent because some venue calendars (e.g. warhol.org) sit behind Cloudflare and reject non-browser clients.

RSS/Atom is accepted only when entries expose structured event fields such as `event_start`, `dtstart`, `venue`, and `location`. The feed publication date is **not** treated as the show date. Missing fields produce an `incomplete` review card.

`MUSICBOT_SQUARESPACE_URLS` accepts Squarespace event-collection URLs (e.g. `https://venue.example/events`) for venues that publish structured JSON but no ICS/RSS — the bot fetches `?format=json` and reads titles, epoch dates, locations, and images from the collection items. The site's own title is used as the venue name. Like the other curated feeds, Squarespace events bypass the affinity gate.

### Spotify (optional)

Create a Spotify developer application. In its settings, register this redirect URI **exactly** (Spotify does not accept `localhost` for this flow):

```text
http://127.0.0.1:8888/callback
```

Then set:

```dotenv
MUSICBOT_SPOTIFY_CLIENT_ID=...
MUSICBOT_SPOTIFY_CLIENT_SECRET=...
MUSICBOT_SPOTIFY_REDIRECT_URI=http://127.0.0.1:8888/callback
```

Authorize the Spotify account whose listening history should supply preferences:

```powershell
music-event-bot spotify-auth --output-file .spotify_refresh_token
```

The command starts a temporary listener on `127.0.0.1`, prints the authorization URL, and normally opens it in a browser. After approval, Spotify redirects to the listener and authorization completes automatically; there is no callback URL to paste into the terminal. Use `--no-browser` to only print the URL or `--timeout-seconds N` to change the five-minute wait.

The browser and callback listener must reach the same host. For Docker or a headless deployment, authorize once from the host environment, then provide the resulting refresh token to the deployment. Copy the token from the temporary output file into `MUSICBOT_SPOTIFY_REFRESH_TOKEN` in the private `.env`, then delete the token file. A refresh token is a secret: never commit it, paste it into chat, or include it in logs. Omitting `--output-file` prints the token to stdout and is only suitable for a private terminal whose output is not captured.

The importer requests `user-top-read` and `user-library-read` and merges three artist signals with manual preferences:

1. **Top artists** across short-, medium-, and long-term listening history.
2. **Top tracks** across the same ranges — this catches artists whose few songs you play constantly, since Spotify exposes no raw play counts.
3. **Liked Songs**: every artist with at least `MUSICBOT_SPOTIFY_LIKED_ARTIST_MIN_TRACKS` saved tracks (default 12, roughly an album's worth). Only the track's primary artist is counted, so features do not inflate totals.

Because a full import walks the entire Liked Songs library (potentially hundreds of requests), the imported artists and genres are persisted in the `taste_preferences` table and reused for `MUSICBOT_SPOTIFY_REFRESH_HOURS` (default 24) between imports. The cache is merge-only — past favorites are never dropped — and any import failure, including Spotify rate limiting, falls back to the cached profile instead of blocking startup.

A refresh token issued before the Liked Songs import only carries `user-top-read`; the importer logs a warning and continues with top artists/tracks until `spotify-auth` is re-run. Spotify has deprecated artist genre metadata, so genre arrays may be empty; artist affinity still works when that happens. OAuth tokens are cached only in memory.

### Last.fm niche genre tags (optional)

RateYourMusic has no public API and prohibits scraping, so niche genre coverage comes from the Last.fm API instead — its community tags use the same vocabulary. Create a free API key at <https://www.last.fm/api/account/create> (no listening history or scrobbling required; tags are global per-artist community data) and set:

```dotenv
MUSICBOT_LASTFM_API_KEY=...
```

At startup the bot fetches the top tags for each profile artist (weight ≥ `MUSICBOT_LASTFM_MIN_TAG_WEIGHT`, up to `MUSICBOT_LASTFM_MAX_TAGS_PER_ARTIST` per fetch, junk tags like "seen live" filtered out) and stores them in SQLite. Tag storage is **append-only**: every `MUSICBOT_LASTFM_TAG_CACHE_DAYS` days (default 90) an artist's tags are re-fetched and merged into the stored set, so an artist whose style evolves accumulates both their old and new genres — nothing is ever forgotten, and an empty or failed refresh cannot lose history. Set the interval to `0` to fetch each artist exactly once instead. The tags join the taste profile's genres directly.

**MusicBrainz fallback:** when no Last.fm key is configured, artist tags come from the MusicBrainz search API instead — no account or key required (`MUSICBOT_MUSICBRAINZ_ENABLED`, default true). MusicBrainz tags are sparser and more conservative than Last.fm's, and its etiquette limits requests to one per second, so the first tag fetch for a large profile takes a few minutes. Tags from both sources share the same append-only cache; adding a Last.fm key later upgrades the source without losing anything already stored.

### Claude genre mapping (optional)

Niche tags rarely match Ticketmaster's broad genre labels, so an optional mapping step sends each **distinct, never-seen** tag to Claude (`MUSICBOT_GENRE_MAP_MODEL`, default `claude-sonnet-5`) once:

```dotenv
MUSICBOT_ANTHROPIC_API_KEY=...
```

Each tag is mapped to (a) the Discord genre buckets from `MUSICBOT_GENRE_ROLE_MAP` and (b) broad Ticketmaster-style genres ("industrial", "metal", "dance/electronic", …) that discovered events actually carry. Results are cached in the `genre_tag_map` table and applied deterministically on every later startup, so a steady-state run makes no model calls. The model never sees or edits event data, and the human approval boundary is unchanged.

## Commands

### CLI

```powershell
music-event-bot migrate
music-event-bot health
music-event-bot events --status pending_review
music-event-bot events --venue "Thunderbird Music Hall" --on 2026-09-13
music-event-bot scrape-once
music-event-bot dedupe
music-event-bot review-sync
music-event-bot seed-profiles
music-event-bot delivery-report --days 30
music-event-bot bot
music-event-bot spotify-auth
```

`seed-profiles` reads who holds which genre role and gives each of them a profile granting the same coverage that role already gave. It is a dry run unless passed `--apply`, and it needs the Server Members intent (enable it in the Developer Portal, then set `MUSICBOT_MEMBERS_INTENT=true`). `delivery-report` replays recent announcements through per-user delivery and reports, per member, how many pings taste alone would give them, how many survive their metro, and how many survive their daily cap.

`health`, `migrate` and `delivery-report` do not require Discord credentials. `scrape-once` works with whichever discovery sources are configured.

`events --venue ... --on ...` answers "does the bot already carry this show?" without going through titles — which is what the sources disagree about. The venue is resolved through `config/venue-aliases.json`, and `--on` is the local calendar night at the venue's own timezone, not the UTC date. The scheduled tasks that write onto the shared Google Calendar call this before adding an entry.

### Duplicate events

`music-event-bot dedupe` reports; `--apply` writes. It does two things:

1. **Repairs derived keys.** The fingerprint is `title | venue | start-minute`, and all three of those get corrected after first sight — most often when a location arrives late and a row ingested with no venue finally gets one. The fingerprint used to be left frozen at first-sight values, which removed that row from dedupe permanently, so the next source describing the same show created a second event and a second review card. Ingest now re-syncs the key automatically; `dedupe` fixes rows written before that and after any change to the venue rules.
2. **Groups events that are one show stored twice** — same venue, starts within `--window-minutes` (default 90), related titles — and folds each group into a single keeper. Published rows win; otherwise the earliest.

Merging is narrow on purpose. A multi-room venue runs different bills at the same hour (Spirit Hall vs Spirit Lodge; Southgate House Revival's three rooms), and a jazz club sells an early and a late set of the same billing about 150 minutes apart — hence the 90-minute default. A duplicate that already announced to Discord is reported but **not** merged unless you pass `--include-published`, because deleting the row does not delete the announcement or the scheduled event; those have to come down by hand.

### Discord slash commands

- `/event submit` — submit a structured event or a URL-backed incomplete event
- `/event show` — inspect an event record
- `/event edit` — open the edit modal
- `/event approve` — approve and publish
- `/event reject` — reject with an optional reason
- `/event retry` — retry a failed publication
- `/event set-role` — map a genre to a Discord role

Review cards also have persistent buttons for the common actions.

### Per-member alert settings

Every member has their own alert settings, and `/me` is how they change them. No permission gate — these are each member's own settings, not a reviewer action.

- `/me show` — current settings, creating a profile from your roles if you have none
- `/me home` — the metro you go to shows in, picked from a list
- `/me travel` — in town, day trip or road trip
- `/me cap` — most pings you want in one day; anything over waits for the catch-up post
- `/me delivery` — mention me on matches, on everything, or never
- `/me genre` — add or drop one of the server's genre buckets
- `/me artist` — follow or unfollow an act

Following an act does not widen what you match — the genre buckets still decide that. It decides which matches survive a busy day: the tail of your daily cap is reserved for shows whose bill includes someone you follow, so the cap gives you the best few rather than the first few. On a quiet day the reserve is never reached and nothing changes.

Dropping a genre or unfollowing an act writes a negative weight rather than deleting the row, so re-joining the matching Discord role does not undo it. Changing anything through `/me` stamps the profile as customized, which is what stops `seed-profiles` handing back a setting you just changed.


## Review and publication lifecycle

```text
discovered -> pending_review -> approved -> published
                |                  |
                |                  -> publish_failed -> retry
                -> rejected

incomplete -> edit -> pending_review
```

Approval places the event in a paced publication queue rather than announcing immediately: up to `MUSICBOT_PUBLISH_BATCH_PER_HOUR` (default 10) approved events publish per hour, soonest show first, and only between `MUSICBOT_PUBLISH_START_HOUR` and `MUSICBOT_PUBLISH_END_HOUR` local time (defaults 06:00–24:00) so nobody is pinged overnight. The queue drains on a 10-minute cadence and an approval triggers an immediate drain attempt, so with budget available an approved event still publishes within moments. Set the batch size to `0` to publish immediately on approval with no pacing. The `Retry publish` button always publishes immediately.

Publication creates the Scheduled Event before posting the public announcement. If Scheduled Event creation fails, no public announcement is sent. Discord IDs are persisted so retries and restarts do not intentionally duplicate publications. The internal event marker is included in Discord descriptions/embeds to recover from a process crash between an external Discord call and the SQLite update.

## Database and backups

SQLite defaults to `data/events.db` and runs with foreign keys, a busy timeout, and WAL mode. Docker Compose mounts `./data` into the container.

Back up these files while the bot is stopped, or use SQLite's online backup tooling:

```text
data/events.db
data/events.db-wal   # if present
data/events.db-shm   # if present
```

## Scheduling

Default discovery covers events from 1 through 180 days ahead and runs at 03:17 in `MUSICBOT_DEFAULT_TIMEZONE`. Review reconciliation runs every 15 minutes, and an immediate discovery/reconciliation cycle runs when the bot connects.

Each reconciliation cycle posts at most `MUSICBOT_REVIEW_POST_BATCH_SIZE` brand-new review cards (default 25), highest score first, so a large first-run backlog drains gradually instead of flooding the review channel; already-posted cards are always kept up to date.

Change the cron expression with:

```dotenv
MUSICBOT_DISCOVERY_CRON=17 3 * * *
MUSICBOT_REVIEW_SYNC_INTERVAL_MINUTES=15
```

## What was not implemented from the original requests

### Facebook Interested/Going events

Not implemented. Ordinary application access to a person's Interested/Going list is restricted, while Selenium login automation is fragile, exposes account credentials, encounters CAPTCHA/2FA, and is unsuitable for a reliable unattended bot.

Recommended workaround: submit Facebook event links through `/event submit` and fill the structured fields during review.

### Instagram following and show scraping

Not implemented. Current official Instagram APIs do not provide a dependable general-purpose following-list workflow for this use case. Browser automation against a personal account would be brittle and credential-sensitive.

Recommended workaround: import artists from Spotify/manual preferences or use an Instagram account data export as the basis for a future one-time importer.

### Generic Live Nation scraping

Not implemented. Live Nation pages are client-rendered and unstable for CSS-selector scraping. Much of its inventory is available through Ticketmaster, which is the supported MVP source.

### Generic OpusOne Productions scraping

Not implemented without a stable public feed or fixture-tested adapter. The original selectors were placeholders. Add an OpusOne or associated-venue ICS/RSS URL to the configured feeds when available; a site-specific adapter can be added later from captured fixtures.

### Bandsintown across arbitrary artists

Not implemented. Broad multi-artist usage depends on the approved scope of the Bandsintown API key and partnership policy. It can be added as a bounded adapter after suitable access is confirmed.

### Arbitrary web-page scraping

Not implemented. A submitted URL is kept as provenance, but arbitrary pages are not trusted as structured event data. This avoids silent date/venue hallucinations and page-specific breakage.

### RateYourMusic genre lookups

Not implemented as such. RYM/Sonemic has no public API (register-interest only since the 2019 beta announcement) and its terms prohibit scraping and automated access. The Last.fm tag import above provides equivalent niche-genre vocabulary through a sanctioned API.

### Fully autonomous AI publishing

Not implemented intentionally. The optional Claude genre-tag mapping (above) is deliberately confined to taste-profile metadata: it runs at import time, is cached, and never generates event data. Publication decisions stay deterministic and the human approval boundary remains mandatory.

## Development and verification

```powershell
python -m compileall -q src
ruff check .
mypy src
pytest
```

Tests use temporary SQLite databases, mocked Ticketmaster responses, feed fixtures, and fake Discord publication gateways. A final real-service smoke test requires the credentials and IDs listed above and should be performed in a staging Discord server before using a production community.

## Local configuration

`config/blocked-artists.json` and `config/venue-aliases.json` are tracked as
**examples**. A running deployment usually wants its own, and a blocklist in
particular records one community's moderation decisions, which do not belong in
a public repository. Keep those local: `config/*.local.json` is gitignored, so
copy the example, edit it, and point the setting at the copy.

```dotenv
MUSICBOT_BLOCKED_ARTISTS_PATH=config/blocked-artists.local.json
```

## License

[MIT](LICENSE).

This project talks to third-party services under their own terms. It uses the
official Ticketmaster Discovery, Spotify, Last.fm, and MusicBrainz APIs, honours
MusicBrainz's one-request-per-second etiquette, and deliberately does **not**
scrape sources whose terms prohibit it — see *What was not implemented from the
original requests* above for the reasoning in each case. Event artwork is
referenced by the source's own URL and is never rehosted.
