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
- Background polling for today's roster transactions and game day inactives.
- Duplicate announcement prevention across container restarts using `/data/state.json`.
- Discord channel and webhook announcement targets.
- Docker Compose setup for home-server hosting.

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
