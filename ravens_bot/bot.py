from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import date
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from .config import BotConfig, load_config, webhook_id
from .dates import parse_user_date, today_in_zone, upcoming_window
from .embeds import (
    error_embed,
    help_embed,
    inactive_embeds,
    next_game_embed,
    schedule_embed,
    standings_embed,
    transaction_embeds,
)
from .espn import EspnApiError, EspnClient
from .models import InactiveReport, Transaction
from .state import AnnouncementState, channel_key


LOGGER = logging.getLogger(__name__)
ScheduleDays = app_commands.Range[int, 1, 30]


@dataclass(frozen=True)
class _AnnouncementTarget:
    key_id: str
    label: str
    destination: discord.abc.Messageable | discord.Webhook


def webhook_label(url: str) -> str:
    return f"webhook {webhook_id(url)}"


class RavensBot(commands.Bot):
    def __init__(self, config: BotConfig) -> None:
        super().__init__(command_prefix=commands.when_mentioned, intents=discord.Intents.default())
        self.config = config
        self.session: aiohttp.ClientSession | None = None
        self.espn: EspnClient | None = None
        self.announcement_state = AnnouncementState(config.state_file)

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession()
        self.espn = EspnClient(self.session)
        self.announcement_state.load()
        self.tree.add_command(_transactions_command(self))
        self.tree.add_command(_inactives_command(self))
        self.tree.add_command(_standings_command(self))
        self.tree.add_command(_next_game_command(self))
        self.tree.add_command(_schedule_command(self))
        self.tree.add_command(_help_command())
        await self.tree.sync()
        if self.config.has_announcement_targets:
            self.poll_updates.change_interval(seconds=self.config.poll_interval_seconds)
            self.poll_updates.start()

    async def close(self) -> None:
        if self.poll_updates.is_running():
            self.poll_updates.cancel()
        if self.session is not None:
            await self.session.close()
        await super().close()

    async def on_ready(self) -> None:
        LOGGER.info("Logged in as %s", self.user)

    @tasks.loop(seconds=300)
    async def poll_updates(self) -> None:
        client = _require_espn(self)
        targets = await self._announcement_targets()
        if not targets:
            return
        target_date = today_in_zone(self.config.time_zone)
        try:
            transactions = await client.fetch_transactions(target_date)
            inactive_reports = await client.fetch_inactives(target_date)
        except EspnApiError as exc:
            LOGGER.warning("Polling skipped because ESPN data could not be fetched: %s", exc)
            return
        await self._post_new_transactions(targets, transactions, target_date)
        await self._post_new_inactives(targets, inactive_reports, target_date)

    async def _post_new_transactions(
        self,
        targets: list[_AnnouncementTarget],
        transactions: list[Transaction],
        target_date: date,
    ) -> None:
        for transaction in transactions:
            key = transaction_announcement_key(transaction)
            embeds = transaction_embeds([transaction], target_date)
            for target in targets:
                if self.announcement_state.unseen(channel_key(key, target.key_id)):
                    await self._announce(target, key, "Ravens roster transaction", embeds)

    async def _post_new_inactives(
        self,
        targets: list[_AnnouncementTarget],
        reports: list[InactiveReport],
        target_date: date,
    ) -> None:
        for report in reports:
            if not report.players:
                continue
            key = inactive_announcement_key(report)
            embeds = inactive_embeds([report], target_date, self.config.time_zone)
            for target in targets:
                if self.announcement_state.unseen(channel_key(key, target.key_id)):
                    await self._announce(target, key, "Ravens game day inactives", embeds)

    async def _announcement_targets(self) -> list[_AnnouncementTarget]:
        targets: list[_AnnouncementTarget] = []
        for channel_id in self.config.discord_channel_ids:
            channel = self.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.fetch_channel(channel_id)
                except discord.DiscordException as exc:
                    LOGGER.warning("Channel %s is unavailable: %s", channel_id, exc)
                    continue
            if isinstance(channel, discord.abc.Messageable):
                targets.append(_AnnouncementTarget(str(channel_id), f"channel {channel_id}", channel))
        for url in self.config.discord_webhook_urls:
            if self.session is None:
                continue
            try:
                webhook = discord.Webhook.from_url(url, session=self.session)
            except (ValueError, discord.DiscordException) as exc:
                LOGGER.warning("Webhook %s is unusable: %s", webhook_label(url), exc)
                continue
            targets.append(_AnnouncementTarget(f"webhook:{webhook_id(url)}", webhook_label(url), webhook))
        return targets

    async def _announce(
        self,
        target: _AnnouncementTarget,
        key: str,
        content: str,
        embeds: list[discord.Embed],
    ) -> None:
        try:
            await target.destination.send(content=content, embeds=embeds)
        except discord.DiscordException as exc:
            LOGGER.warning("Could not post to %s: %s", target.label, exc)
            return
        self.announcement_state.mark(channel_key(key, target.key_id))

    @poll_updates.before_loop
    async def before_poll_updates(self) -> None:
        await self.wait_until_ready()


def _transactions_command(bot: RavensBot) -> app_commands.Command[Any, ..., None]:
    @app_commands.command(name="transactions", description="Show Ravens roster transactions for a date.")
    @app_commands.describe(date="Optional date: today or YYYY-MM-DD")
    async def transactions(interaction: discord.Interaction, date: str | None = None) -> None:
        target_date = await _parse_or_respond(interaction, date, bot.config)
        if target_date is None:
            return
        await interaction.response.defer(ephemeral=True)
        try:
            items = await _require_espn(bot).fetch_transactions(target_date)
        except EspnApiError as exc:
            await interaction.followup.send(embed=error_embed(str(exc)), ephemeral=True)
            return
        await interaction.followup.send(embeds=transaction_embeds(items, target_date))

    return transactions


def _inactives_command(bot: RavensBot) -> app_commands.Command[Any, ..., None]:
    @app_commands.command(name="inactives", description="Show Ravens game day inactives for a date.")
    @app_commands.describe(date="Optional date: today or YYYY-MM-DD")
    async def inactives(interaction: discord.Interaction, date: str | None = None) -> None:
        target_date = await _parse_or_respond(interaction, date, bot.config)
        if target_date is None:
            return
        await interaction.response.defer(ephemeral=True)
        try:
            reports = await _require_espn(bot).fetch_inactives(target_date)
        except EspnApiError as exc:
            await interaction.followup.send(embed=error_embed(str(exc)), ephemeral=True)
            return
        await interaction.followup.send(embeds=inactive_embeds(reports, target_date, bot.config.time_zone))

    return inactives


def _standings_command(bot: RavensBot) -> app_commands.Command[Any, ..., None]:
    @app_commands.command(name="standings", description="Show AFC North standings.")
    async def standings(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            items = await _require_espn(bot).fetch_standings()
        except EspnApiError as exc:
            await interaction.followup.send(embed=error_embed(str(exc)), ephemeral=True)
            return
        await interaction.followup.send(embed=standings_embed(items))

    return standings


def _next_game_command(bot: RavensBot) -> app_commands.Command[Any, ..., None]:
    @app_commands.command(name="nextgame", description="Show who the Ravens play next.")
    async def nextgame(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            game = await _require_espn(bot).fetch_next_game(today_in_zone(bot.config.time_zone))
        except EspnApiError as exc:
            await interaction.followup.send(embed=error_embed(str(exc)), ephemeral=True)
            return
        await interaction.followup.send(embed=next_game_embed(game, bot.config.time_zone))

    return nextgame


def _schedule_command(bot: RavensBot) -> app_commands.Command[Any, ..., None]:
    @app_commands.command(name="schedule", description="Show upcoming Ravens games.")
    @app_commands.describe(days="How many days ahead to show (1-30, default 7)")
    async def schedule(interaction: discord.Interaction, days: ScheduleDays = 7) -> None:
        try:
            window = upcoming_window(days, bot.config.time_zone)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            games = await _require_espn(bot).fetch_schedule(window)
        except EspnApiError as exc:
            await interaction.followup.send(embed=error_embed(str(exc)), ephemeral=True)
            return
        await interaction.followup.send(embed=schedule_embed(games, bot.config.time_zone))

    return schedule


def _help_command() -> app_commands.Command[Any, ..., None]:
    @app_commands.command(name="help", description="Show Ravens bot command help.")
    async def help_command(interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=help_embed(), ephemeral=True)

    return help_command


async def _parse_or_respond(
    interaction: discord.Interaction, raw_date: str | None, config: BotConfig
) -> date | None:
    try:
        return parse_user_date(raw_date, config.time_zone)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return None


def _require_espn(bot: RavensBot) -> EspnClient:
    if bot.espn is None:
        raise RuntimeError("ESPN client is not initialized")
    return bot.espn


def transaction_announcement_key(transaction: Transaction) -> str:
    return f"transaction:{transaction.date.isoformat()}:{transaction.transaction_id}"


def inactive_announcement_key(report: InactiveReport) -> str:
    players = ",".join(sorted(player.name for player in report.players))
    return f"inactives:{report.game.event_id}:{players}"


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    try:
        config = load_config()
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    bot = RavensBot(config)
    try:
        bot.run(config.discord_token, log_handler=None)
    except (KeyboardInterrupt, asyncio.CancelledError):
        LOGGER.info("Shutting down")
