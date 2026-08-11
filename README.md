# The-Castle

A Dockerized Python Discord bot for Baltimore Ravens roster transactions, game
day inactives, standings, and upcoming games.

## Features

- Slash commands:
  - `/transactions [date]` — Ravens roster transactions for today or a `YYYY-MM-DD` date.
  - `/inactives [date]` — game day inactive reports when ESPN publishes them.
  - `/standings` — AFC North standings, with the Ravens highlighted.
  - `/nextgame` — the next Ravens matchup.
  - `/schedule [days]` — upcoming Ravens games over the next 1-30 days.
  - `/help` — command help.
- Rich embeds: team logos, player headshots, and clickable links out to ESPN
  player, team, and game pages.
- Roster-backed player resolution, so names in a transaction become real links.
- Background polling for today's roster transactions and game day inactives.
- Duplicate announcement prevention across container restarts using `/data/state.json`.
- Discord channel and webhook announcement targets.
- Docker Compose setup for home-server hosting.

## Embeds

Every response is an embed built in `ravens_bot/embeds.py` from the plain-text
helpers in `ravens_bot/formatting.py`. Keeping rendering separate from Discord
means the wording is unit tested without constructing a client.

- **Transactions** list one field per move. ESPN's NFL transaction feed carries
  only prose — no athlete record — so player names and positions are parsed out
  of the description and matched against the team roster to recover ESPN ids.
  A move about a single player is posted with their full-size headshot; a move
  covering several players falls back to a thumbnail, since one face would
  misrepresent the post. A mass roster cut skips link markup entirely, because
  twenty links would crowd the wording out of the field's character budget.
- **Standings** show record, win percentage, games back, and streak per team,
  with the Ravens bolded, plus division, conference, home, and away splits and a
  footer summarising where the Ravens sit.
- **Games** show kickoff, broadcast, venue, week, and both records, and use the
  opponent's logo, since the Ravens appear in every post.
- **Inactives** are grouped by team with position and reason.

Embeds are truncated to Discord's limits rather than being rejected at send
time, and any list longer than 25 fields states how many entries were hidden.

## Caching

`ravens_bot/cache.py` holds a small TTL cache in front of the slower endpoints:
standings for 5 minutes, the schedule for 3, and the roster for an hour. Each
key has its own lock, so a burst of commands on a cold key waits on one in-flight
request instead of issuing several identical ones.

## Setup

1. Create a Discord application and bot at <https://discord.com/developers/applications>.
2. Copy `.env.example` to `.env`.
3. Set `DISCORD_TOKEN`.
4. Set `DISCORD_CHANNEL_ID`, `DISCORD_WEBHOOK_URL`, or both for background posts.
5. Start the bot:

```bash
docker compose up --build
```

The compose file mounts a named volume at `/data` so announcement state survives
container restarts.

## Environment variables

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `DISCORD_TOKEN` | Yes | | Discord bot token. Never commit it. |
| `DISCORD_CHANNEL_ID` | No | | One channel ID, or several separated by commas, for background announcements. |
| `DISCORD_WEBHOOK_URL` | No | | One Discord webhook URL, or several separated by commas, for background announcements. |
| `POLL_INTERVAL_SECONDS` | No | `300` | Poll interval for automatic announcements. Minimum 30 seconds. |
| `TIME_ZONE` | No | `America/New_York` | Time zone used for "today" and display times. |

## Discord permissions and intents

Use the OAuth2 URL generator in the Discord developer portal:

- Scopes: `bot`, `applications.commands`
- Bot permissions: `Send Messages`, `Embed Links`, `Use Slash Commands`
- Privileged gateway intents are not required.

Slash commands are synced globally when the bot starts. Discord can take several
minutes to make new global commands visible.

## Development

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Run checks:

```bash
python -m pytest tests
python -m compileall ravens_bot tests
```

Tests use local sample payloads and do not call the network.

## Data source

This project uses ESPN's public NFL site/core APIs. It does not require API keys.

Two ESPN behaviours are worth knowing, since both look like bugs otherwise:

- A transactions query for one date also returns the following day's moves. Each
  item is stamped at midnight Pacific on the day it happened, so the parser keeps
  only the date it asked for; accepting the extra day would report the same move
  on two consecutive dates.
- The `teams` query parameter on the transactions endpoint is ignored — the feed
  comes back league-wide either way — so Ravens moves are filtered client side.
