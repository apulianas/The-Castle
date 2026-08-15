from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks

from .config import BotConfig, load_config, webhook_id
from .dates import (
    MAX_SCHEDULE_DAYS,
    now_in_zone,
    parse_user_date,
    today_in_zone,
    upcoming_window,
)
from .embeds import (
    error_embed,
    field_goal_embed,
    fourth_down_embed,
    help_embed,
    inactive_embeds,
    injury_embeds,
    live_game_embed,
    next_game_embed,
    no_field_goal_embed,
    no_fourth_down_embed,
    no_live_game_embed,
    no_snap_counts_embed,
    player_snap_embed,
    player_snap_totals_embed,
    roster_news_post,
    schedule_embed,
    snap_count_embed,
    snap_totals_embed,
    standings_embed,
    transaction_embeds,
)
from .espn import (
    EspnApiError,
    EspnClient,
    combine_roster_news,
    match_team_games,
    select_insight_game,
    team_names,
)
from .fourthdown import (
    LONGEST_ASKABLE_FIELD_GOAL,
    MIN_FIELD_GOAL_YARDS,
    advise,
    field_goal_outlook,
)
from .formatting import (
    format_no_ball_spot,
    format_no_field_goal_spot,
    format_no_live_game,
    format_no_snap_counts,
    format_no_snap_games,
    format_not_fourth_down,
    format_unknown_snap_player,
    format_unknown_team,
)
from .models import (
    Game,
    InactiveReport,
    InjuryReport,
    InjuryUpdate,
    PlayerRef,
    SnapCountReport,
    Transaction,
)
from .snapcounts import (
    MAX_SNAP_GAMES,
    SnapCountClient,
    SnapCountError,
    aggregate,
    match_players,
)
from .recall import FourthDownMemory, RememberedSituation
from .state import AnnouncementState, channel_key


LOGGER = logging.getLogger(__name__)
ScheduleDays = app_commands.Range[int, 1, MAX_SCHEDULE_DAYS]
SnapWeeks = app_commands.Range[int, 1, MAX_SNAP_GAMES]
KickYards = app_commands.Range[int, MIN_FIELD_GOAL_YARDS, LONGEST_ASKABLE_FIELD_GOAL]
# How often the scoreboard is read to record fourth downs, so the question can
# still be answered once the play is over.
TRACK_INTERVAL_SECONDS = 30
# When nothing is being played there is nothing to record, so the tracker sits
# out this many ticks — five minutes — before looking again.
IDLE_TRACK_TICKS = 9
# How many close names a failed player search offers back.
MAX_PLAYER_SUGGESTIONS = 5
# How many live teams a failed team search offers back.
MAX_TEAM_SUGGESTIONS = 8
# Announcement keys for injury posts, shared by the first-run check.
INJURY_KEY_PREFIX = "injury:"


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
        self.snap_counts: SnapCountClient | None = None
        self.announcement_state = AnnouncementState(config.state_file)
        self.fourth_downs = FourthDownMemory()
        self._idle_track_ticks = 0

    async def setup_hook(self) -> None:
        self.session = aiohttp.ClientSession()
        self.espn = EspnClient(self.session)
        self.snap_counts = SnapCountClient(self.session)
        self.announcement_state.load()
        self.tree.add_command(_transactions_command(self))
        self.tree.add_command(_inactives_command(self))
        self.tree.add_command(_injuries_command(self))
        self.tree.add_command(_standings_command(self))
        self.tree.add_command(_next_game_command(self))
        self.tree.add_command(_live_command(self))
        self.tree.add_command(_schedule_command(self))
        self.tree.add_command(_snapcounts_command(self))
        self.tree.add_command(_fourthdown_command(self))
        self.tree.add_command(_fieldgoal_command(self))
        self.tree.add_command(_help_command())
        await self.tree.sync()
        if self.config.has_announcement_targets:
            self.poll_updates.change_interval(seconds=self.config.poll_interval_seconds)
            self.poll_updates.start()
        self.track_fourth_downs.start()

    async def close(self) -> None:
        if self.poll_updates.is_running():
            self.poll_updates.cancel()
        if self.track_fourth_downs.is_running():
            self.track_fourth_downs.cancel()
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
            injuries = await client.fetch_injuries()
        except EspnApiError as exc:
            LOGGER.warning("Polling skipped because ESPN data could not be fetched: %s", exc)
            return
        await self._post_new_roster_news(targets, transactions, injuries, target_date)
        await self._post_new_inactives(targets, inactive_reports, target_date)

    async def _post_new_roster_news(
        self,
        targets: list[_AnnouncementTarget],
        transactions: list[Transaction],
        report: InjuryReport,
        target_date: date,
    ) -> None:
        """Post today's moves and injury changes, merging the ones that overlap.

        A move and the injury report entry it produces are the same news, so a
        player activated off injured reserve is announced once rather than as a
        roster move and a status change minutes apart.
        """
        for target in targets:
            first_run = bool(report.updates) and not self.announcement_state.has_target_keys(
                INJURY_KEY_PREFIX, target.key_id
            )
            if first_run:
                await self._announce_injury_report(target, report)
            # A first run has just posted the standing report in one message, so
            # there is no injury news left for today's moves to carry.
            updates = () if first_run else report.updates
            moves, standalone = combine_roster_news(
                [
                    transaction
                    for transaction in transactions
                    if self._unseen(target, transaction_announcement_key(transaction))
                ],
                [
                    update
                    for update in updates
                    if self._unseen(target, injury_announcement_key(update))
                ],
            )
            for news in moves:
                embeds, carried = roster_news_post(news, target_date)
                await self._announce(
                    target,
                    [
                        transaction_announcement_key(news.transaction),
                        *(injury_announcement_key(update) for update in carried),
                    ],
                    embeds,
                )
            for update in standalone:
                await self._announce(
                    target,
                    [injury_announcement_key(update)],
                    injury_embeds(InjuryReport((update,))),
                )

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
                if self._unseen(target, key):
                    await self._announce(target, [key], embeds)

    async def _announce_injury_report(
        self, target: _AnnouncementTarget, report: InjuryReport
    ) -> None:
        """One consolidated post the first time a target sees an injury report.

        Without this a fresh state file would post a message per player already
        listed, which is a dozen notifications for news nobody is waiting on.
        """
        await self._announce(
            target,
            [injury_announcement_key(update) for update in report.updates],
            injury_embeds(report),
        )

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

    def _unseen(self, target: _AnnouncementTarget, key: str) -> bool:
        return self.announcement_state.unseen(channel_key(key, target.key_id))

    async def _announce(
        self,
        target: _AnnouncementTarget,
        keys: Sequence[str],
        embeds: list[discord.Embed],
    ) -> None:
        """Post to one target and record every piece of news the post covers.

        The embed is the whole message: a line of text above it would only
        restate the title Discord is already showing.

        A failed post records nothing, so the next poll tries it again.
        """
        try:
            await target.destination.send(embeds=embeds)
        except discord.DiscordException as exc:
            LOGGER.warning("Could not post to %s: %s", target.label, exc)
            return
        for key in keys:
            self.announcement_state.mark(channel_key(key, target.key_id))

    @poll_updates.before_loop
    async def before_poll_updates(self) -> None:
        await self.wait_until_ready()

    @tasks.loop(seconds=TRACK_INTERVAL_SECONDS)
    async def track_fourth_downs(self) -> None:
        """Record live fourth downs so one can be recalled after the play.

        A fourth down is over in under a minute, and the person arguing about it
        types the command afterwards, so waiting for someone to ask would record
        nothing. Nothing is fetched while no game is on: the scoreboard is one
        cached request either way, and the tracker sits out most of the week.
        """
        if self._idle_track_ticks > 0:
            self._idle_track_ticks -= 1
            return
        try:
            games = await _require_espn(self).fetch_live_games()
        except EspnApiError as exc:
            LOGGER.debug("Fourth down tracking skipped: %s", exc)
            self._idle_track_ticks = IDLE_TRACK_TICKS
            return
        self.fourth_downs.remember(games)
        self._idle_track_ticks = 0 if games else IDLE_TRACK_TICKS

    @track_fourth_downs.before_loop
    async def before_track_fourth_downs(self) -> None:
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


def _injuries_command(bot: RavensBot) -> app_commands.Command[Any, ..., None]:
    @app_commands.command(name="injuries", description="Show the Ravens injury report.")
    async def injuries(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            report = await _require_espn(bot).fetch_injuries()
        except EspnApiError as exc:
            await interaction.followup.send(embed=error_embed(str(exc)), ephemeral=True)
            return
        await interaction.followup.send(embeds=injury_embeds(report))

    return injuries


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
    @app_commands.describe(
        days=f"How many days ahead to show (1-{MAX_SCHEDULE_DAYS}, default 7)"
    )
    async def schedule(interaction: discord.Interaction, days: ScheduleDays = 7) -> None:
        try:
            window = upcoming_window(days, bot.config.time_zone)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        try:
            games = await _require_espn(bot).fetch_upcoming(window, bot.config.time_zone)
        except EspnApiError as exc:
            await interaction.followup.send(embed=error_embed(str(exc)), ephemeral=True)
            return
        await interaction.followup.send(embed=schedule_embed(games, bot.config.time_zone, days))

    return schedule


def _snapcounts_command(bot: RavensBot) -> app_commands.Command[Any, ..., None]:
    @app_commands.command(name="snapcounts", description="Show Ravens snap counts.")
    @app_commands.describe(
        player="Optional player name; omit for the full team report",
        weeks=f"How many recent games to cover (1-{MAX_SNAP_GAMES}, default 1)",
    )
    async def snapcounts(
        interaction: discord.Interaction,
        player: str | None = None,
        weeks: SnapWeeks = 1,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            reports = await _recent_snap_reports(bot, weeks)
        except EspnApiError as exc:
            await interaction.followup.send(embed=error_embed(str(exc)), ephemeral=True)
            return
        except SnapCountError as exc:
            await interaction.followup.send(embed=error_embed(str(exc)), ephemeral=True)
            return

        if reports is None:
            await interaction.followup.send(
                embed=no_snap_counts_embed(format_no_snap_games())
            )
            return
        if not reports:
            await interaction.followup.send(
                embed=no_snap_counts_embed(format_no_snap_counts())
            )
            return

        totals = aggregate(reports)
        if player is None:
            if weeks == 1:
                await interaction.followup.send(embed=snap_count_embed(reports[-1]))
            else:
                await interaction.followup.send(
                    embed=snap_totals_embed(totals, reports, weeks)
                )
            return

        matches = match_players(totals, player)
        if len(matches) != 1:
            suggestions = [item.player.name for item in matches][
                :MAX_PLAYER_SUGGESTIONS
            ]
            await interaction.followup.send(
                embed=no_snap_counts_embed(
                    format_unknown_snap_player(player, suggestions)
                ),
                ephemeral=True,
            )
            return

        match = matches[0]
        if weeks == 1:
            report = reports[-1]
            entry = next(
                (item for item in report.players if item.player.name == match.player.name),
                None,
            )
            if entry is None:
                await interaction.followup.send(
                    embed=no_snap_counts_embed(format_no_snap_counts(report.game)),
                    ephemeral=True,
                )
                return
            await interaction.followup.send(embed=player_snap_embed(entry, report))
            return
        await interaction.followup.send(
            embed=player_snap_totals_embed(match, reports, weeks)
        )

    return snapcounts


def _fourthdown_command(bot: RavensBot) -> app_commands.Command[Any, ..., None]:
    @app_commands.command(
        name="fourthdown",
        description="Should the team with the ball go for it, kick, or punt?",
    )
    @app_commands.describe(
        team="Optional team; omit for the Ravens game, or whatever else is live"
    )
    async def fourthdown(
        interaction: discord.Interaction, team: str | None = None
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            games = await _live_games(bot)
        except EspnApiError as exc:
            await interaction.followup.send(embed=error_embed(str(exc)), ephemeral=True)
            return

        game, problem = _select_game(bot, games, team)
        if game is not None:
            situation = game.situation
            advice = advise(situation) if situation is not None else None
            if advice is not None and advice.can_advise:
                await interaction.followup.send(embed=fourth_down_embed(game, advice))
                return

        # The play is over by the time most people type this, so the last fourth
        # down seen answers rather than "that is not a fourth down".
        remembered = _remembered_fourth_down(bot, game, team)
        if remembered is not None:
            recalled = advise(remembered.situation)
            if recalled.can_advise:
                await interaction.followup.send(
                    embed=fourth_down_embed(
                        remembered.game, recalled, remembered.age_seconds
                    )
                )
                return

        if game is None:
            await interaction.followup.send(
                embed=no_fourth_down_embed(problem or format_no_live_game()),
                ephemeral=True,
            )
            return
        reason = "ESPN has not published a down for this game yet."
        if game.situation is not None:
            reason = advise(game.situation).reason or reason
        await interaction.followup.send(
            embed=no_fourth_down_embed(format_not_fourth_down(game, reason), game),
            ephemeral=True,
        )

    return fourthdown


def _fieldgoal_command(bot: RavensBot) -> app_commands.Command[Any, ..., None]:
    @app_commands.command(
        name="fieldgoal",
        description="How often a field goal of this length is made, and what it is worth.",
    )
    @app_commands.describe(
        yards="Kick distance in yards; omit to use where the ball is now",
        team="Optional team; omit for the Ravens game, or whatever else is live",
    )
    async def fieldgoal(
        interaction: discord.Interaction,
        yards: KickYards | None = None,
        team: str | None = None,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            games = await _live_games(bot)
        except EspnApiError as exc:
            if yards is None:
                await interaction.followup.send(
                    embed=error_embed(str(exc)), ephemeral=True
                )
                return
            # A stated distance needs no game, so an ESPN outage answers anyway.
            games = []

        game, problem = _select_game(bot, games, team)
        if yards is not None:
            await interaction.followup.send(
                embed=field_goal_embed(field_goal_outlook(kick_distance=yards), game)
            )
            return

        if game is None:
            await interaction.followup.send(
                embed=no_field_goal_embed(problem or format_no_field_goal_spot()),
                ephemeral=True,
            )
            return
        situation = game.situation
        if situation is None or situation.yards_to_goal is None:
            await interaction.followup.send(
                embed=no_field_goal_embed(format_no_ball_spot(game), game),
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            embed=field_goal_embed(
                field_goal_outlook(
                    yards_to_goal=situation.yards_to_goal, situation=situation
                ),
                game,
            )
        )

    return fieldgoal


async def _live_games(bot: RavensBot) -> list[Game]:
    """Every game in progress, recording any fourth down on the way past."""
    games = await _require_espn(bot).fetch_live_games()
    bot.fourth_downs.remember(games)
    return games


def _select_game(
    bot: RavensBot, games: list[Game], team: str | None
) -> tuple[Game | None, str | None]:
    """The game a live question is about, or why there is not one."""
    if not games:
        return None, format_no_live_game()
    if team:
        matches = match_team_games(games, team)
        if not matches:
            return None, format_unknown_team(
                team, team_names(games)[:MAX_TEAM_SUGGESTIONS]
            )
        return matches[0], None
    game = select_insight_game(
        games, bot.config.secondary_team, now_in_zone(bot.config.time_zone)
    )
    return game, None if game is not None else format_no_live_game()


def _remembered_fourth_down(
    bot: RavensBot, game: Game | None, team: str | None
) -> RememberedSituation | None:
    """The last fourth down recorded for this game, or the freshest one seen."""
    if game is not None:
        return bot.fourth_downs.recall(game.event_id)
    if team:
        matches = match_team_games(bot.fourth_downs.games(), team)
        if not matches:
            return None
        return bot.fourth_downs.recall(matches[0].event_id)
    return bot.fourth_downs.latest()


async def _recent_snap_reports(bot: RavensBot, weeks: int) -> list[SnapCountReport] | None:
    """Reports for the last ``weeks`` completed games, or None when none exist."""
    espn = _require_espn(bot)
    games = await espn.fetch_recent_games(weeks, today_in_zone(bot.config.time_zone))
    if not games:
        return None
    roster: dict[str, PlayerRef] = {}
    try:
        roster = await espn.fetch_roster()
    except EspnApiError:
        # Player art and links are a bonus; a roster outage should not hide the
        # snap counts themselves.
        LOGGER.warning("Snap counts posted without roster art")
    return await _require_snap_counts(bot).fetch_reports(games, roster)


def _live_command(bot: RavensBot) -> app_commands.Command[Any, ..., None]:
    @app_commands.command(
        name="live", description="Show live in-game stats for the Ravens."
    )
    async def live(interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        today = today_in_zone(bot.config.time_zone)
        try:
            report = await _require_espn(bot).fetch_live_game(today)
        except EspnApiError as exc:
            await interaction.followup.send(embed=error_embed(str(exc)), ephemeral=True)
            return
        if report is None:
            await interaction.followup.send(embed=no_live_game_embed(today))
            return
        await interaction.followup.send(
            embed=live_game_embed(report, bot.config.time_zone)
        )

    return live


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


def _require_snap_counts(bot: RavensBot) -> SnapCountClient:
    if bot.snap_counts is None:
        raise RuntimeError("Snap count client is not initialized")
    return bot.snap_counts


def transaction_announcement_key(transaction: Transaction) -> str:
    return f"transaction:{transaction.transaction_id}"


def injury_announcement_key(update: InjuryUpdate) -> str:
    return f"{INJURY_KEY_PREFIX}{update.announcement_id}"


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
