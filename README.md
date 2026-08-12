# The-Castle

A Dockerized Python Discord bot for Baltimore Ravens roster transactions, game
day inactives, standings, live in-game stats, and upcoming games.

## Features

- Slash commands:
  - `/transactions [date]` — Ravens roster transactions for today or a `YYYY-MM-DD` date.
  - `/inactives [date]` — game day inactive reports when ESPN publishes them.
  - `/standings` — AFC North standings, with the Ravens highlighted.
  - `/nextgame` — the next Ravens matchup.
  - `/live` — live score, clock, possession, team totals, and leaders for today's game.
  - `/schedule [days]` — upcoming Ravens games over the next 1-30 days.
  - `/snapcounts [player] [weeks]` — snap counts for the last game, or the last 1-18 games.
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
- **Live stats** lead with the score, clock, quarter, possession, and down and
  distance, then list both teams' box score totals side by side and a leading
  player per category, Ravens first. A game that has not kicked off points at
  `/nextgame` instead, since there is nothing to report yet, and a finished game
  shows the same layout as a live one, which is what a final box score is. ESPN
  publishes these sections at different points in a game, so the post degrades
  to the score and clock rather than failing when a section is missing. The
  footer states when the snapshot was taken, because the numbers move.
- **Snap counts** list players by unit — offence, defence, special teams — each
  in the unit they played most, sorted by snaps. A player is a line inside a
  unit's field rather than a field of their own, because a full report names
  forty players and Discord allows twenty five fields. Naming a player switches
  to their own embed, with their headshot and, over several games, a week by
  week breakdown.

Embeds are truncated to Discord's limits rather than being rejected at send
time, and any list longer than 25 fields states how many entries were hidden.

## Caching

`ravens_bot/cache.py` holds a small TTL cache in front of the slower endpoints:
standings for 5 minutes, the schedule for 3, the roster for an hour, a game
summary for 45 seconds, and a season of snap counts for 6 hours, since a
finished game's snaps never change and the file only grows a week at a time. A
live game moves play by play, so its short entry exists to absorb a burst of
`/live` calls rather than to spare ESPN the traffic; the score shown always
comes from the summary, which leads the cached scoreboard. Each key has its own
lock, so a burst of commands on a cold key waits on one in-flight request
instead of issuing several identical ones.

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

### Snap counts

ESPN does not publish snap counts. They come from the NFL's GSIS game book,
whose player participation page is posted per game at
`https://nflgsis.com/{season}/{Reg|Post}/{week:02d}/{gamekey}/Gamebook.pdf`.

`ravens_bot/snapcounts.py` does not read that PDF. Doing so would mean shipping
a PDF text extractor, mapping ESPN's event id onto the GSIS game key the URL
needs, and re-deriving each percentage by hand from a page whose layout carries
no guarantee. The sibling `Gamebook.xml` does not help: it names starters,
substitutions, and inactives, but carries no snap totals.

nflverse republishes the same participation numbers as a per-season CSV keyed by
season, week, and team, and that is what the bot reads. It needs no PDF
dependency and no game key, and it carries the unit percentages the game book
prints beside the counts.

Three consequences are worth knowing:

- Snap counts trail the final whistle by hours, so a game with no published
  numbers is reported as pending rather than as an error.
- The file states each player's share of a unit rather than the unit's total, so
  the denominator is rebuilt from the counts and shares and the value most of a
  unit agrees on is used. A count larger than that total is printed on its own
  rather than as a share above 100%.
- A Ravens game is matched to the file by season, opponent, and whether the
  Ravens were at home, with the regular season flag separating a playoff rematch
  from the regular season meeting it repeats.
