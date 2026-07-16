---
name: verify
summary: Drive the CLI against isolated SQLite state and local event feeds.
---

# Verify the music event bot

Use a temporary SQLite database and serve `tests/fixtures/` over a loopback HTTP server. Set `MUSICBOT_ICS_URLS` to the local sample calendar, then drive these public CLI surfaces:

1. `music-event-bot migrate`
2. `music-event-bot health`
3. `music-event-bot scrape-once`
4. `music-event-bot events --status pending_review`
5. Run `scrape-once` a second time and confirm the event count remains one.

Also probe missing Discord configuration with `music-event-bot bot`; it should fail before connecting and name the required settings. Never use production credentials for verification. Real Discord Scheduled Event creation remains a staging-guild check.
