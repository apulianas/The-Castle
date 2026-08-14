from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ravens_bot.bot import injury_announcement_key
from ravens_bot.embeds import injury_embeds
from ravens_bot.espn import apply_roster_to_injuries, parse_injuries
from ravens_bot.espn_urls import injuries_url, team_logo_url
from ravens_bot.formatting import format_injury, format_no_injuries
from ravens_bot.models import (
    RAVENS_SLUG,
    InjuryReport,
    InjuryUpdate,
    PlayerRef,
    injury_status_rank,
)
from ravens_bot.state import AnnouncementState, channel_key


TIME_ZONE = ZoneInfo("America/New_York")


def _payload() -> dict[str, object]:
    return {
        "injuries": [
            {
                "id": "1",
                "athlete": {
                    "id": "4426354",
                    "displayName": "Zay Flowers",
                    "position": {"abbreviation": "WR", "displayName": "Wide Receiver"},
                    "headshot": {"href": "https://a.espncdn.com/flowers.png"},
                },
                "status": "Questionable",
                "details": {"type": "Knee", "side": "Left", "returnDate": "2026-09-13"},
                "shortComment": "Limited in practice Thursday.",
                "date": "2026-09-10T18:41Z",
            },
            {
                "id": "2",
                "athlete": {"id": "3916387", "displayName": "Ronnie Stanley"},
                "type": {"description": "Out"},
                "details": {"type": "Ankle"},
                "longComment": "Stanley did not practice.",
                "date": "2026-09-10T19:00Z",
            },
        ]
    }


def test_parse_injuries_reads_players_status_and_detail() -> None:
    report = parse_injuries(_payload())

    assert [update.player.name for update in report.updates] == [
        "Ronnie Stanley",
        "Zay Flowers",
    ]
    out, questionable = report.updates
    assert out.status == "Out"
    assert out.detail == "Ankle"
    assert out.comment == "Stanley did not practice."
    assert questionable.detail == "Knee - Left"
    assert questionable.player.position == "WR"
    assert questionable.player.headshot == "https://a.espncdn.com/flowers.png"
    assert questionable.return_date == "2026-09-13"
    assert report.last_updated == datetime(2026, 9, 10, 19, 0, tzinfo=timezone.utc)


def test_parse_injuries_reads_grouped_team_payload() -> None:
    payload = {
        "injuries": [
            {
                "id": "33",
                "displayName": "Baltimore Ravens",
                "injuries": [
                    {
                        "athlete": {"id": "1", "displayName": "Kyle Hamilton"},
                        "status": {"name": "Doubtful"},
                    }
                ],
            }
        ]
    }

    report = parse_injuries(payload)

    assert [update.player.name for update in report.updates] == ["Kyle Hamilton"]
    assert report.updates[0].status == "Doubtful"


def test_parse_injuries_reads_resolved_items_payload() -> None:
    payload = {
        "items": [
            {
                "athlete": {"id": "7", "displayName": "Roquan Smith"},
                "status": "Injured Reserve",
            }
        ]
    }

    assert [update.player.name for update in parse_injuries(payload).updates] == [
        "Roquan Smith"
    ]


def test_parse_injuries_skips_entries_without_an_athlete() -> None:
    payload = {"injuries": [{"status": "Out"}, {"athlete": {}, "status": "Out"}]}

    assert parse_injuries(payload).updates == ()


def test_parse_injuries_keeps_entries_without_a_status() -> None:
    payload = {"injuries": [{"athlete": {"id": "9", "displayName": "Nate Wiggins"}}]}

    update = parse_injuries(payload).updates[0]

    assert update.status is None
    assert update.status_text == "Unknown"


def test_parse_injuries_drops_duplicate_players() -> None:
    payload = {
        "injuries": [
            {"athlete": {"id": "9", "displayName": "Nate Wiggins"}, "status": "Out"},
            {"athlete": {"id": "9", "displayName": "Nate Wiggins"}, "status": "Out"},
        ]
    }

    assert len(parse_injuries(payload).updates) == 1


def test_parse_injuries_on_an_empty_payload() -> None:
    assert parse_injuries({}).updates == ()
    assert parse_injuries({"injuries": []}).last_updated is None


def test_injury_status_rank_orders_out_before_questionable() -> None:
    assert injury_status_rank("Out") < injury_status_rank("Doubtful")
    assert injury_status_rank("Doubtful") < injury_status_rank("Questionable")
    assert injury_status_rank("Questionable") < injury_status_rank("Injured Reserve")
    assert injury_status_rank(None) == injury_status_rank("Something Else")


def test_apply_roster_fills_in_position_and_headshot() -> None:
    report = parse_injuries(
        {"injuries": [{"athlete": {"id": "2", "displayName": "Ronnie Stanley"}}]}
    )
    roster = {
        "ronnie stanley": PlayerRef(
            name="Ronnie Stanley",
            athlete_id="2",
            position="OT",
            headshot="https://a.espncdn.com/stanley.png",
        )
    }

    resolved = apply_roster_to_injuries(report, roster).updates[0]

    assert resolved.player.position == "OT"
    assert resolved.player.headshot == "https://a.espncdn.com/stanley.png"


def test_apply_roster_leaves_unknown_players_alone() -> None:
    report = parse_injuries(
        {"injuries": [{"athlete": {"id": "2", "displayName": "Ronnie Stanley"}}]}
    )

    assert apply_roster_to_injuries(report, {}).updates == report.updates


def test_format_injury_names_the_injury_and_expected_return() -> None:
    update = parse_injuries(_payload()).updates[1]

    text = format_injury(update)

    assert "[Zay Flowers](" in text
    assert text.startswith("WR ")
    assert "Knee - Left" in text
    assert "Limited in practice Thursday." in text
    assert "expected back September 13, 2026" in text


def test_format_injury_without_details_is_just_the_player() -> None:
    update = InjuryUpdate(player=PlayerRef(name="Kyle Hamilton"), status="Out")

    assert format_injury(update) == "Kyle Hamilton"


def test_injury_embed_groups_by_status_with_a_team_thumbnail() -> None:
    report = parse_injuries(_payload())

    embed = injury_embeds(report, TIME_ZONE)[0]

    assert embed.title == "Ravens injury report"
    assert embed.url == injuries_url(RAVENS_SLUG)
    assert [field.name for field in embed.fields] == ["Out (1)", "Questionable (1)"]
    assert embed.thumbnail.url == team_logo_url(RAVENS_SLUG)
    assert embed.image.url is None
    assert "Updated" in (embed.description or "")


def test_injury_embed_for_one_player_uses_a_small_headshot() -> None:
    report = InjuryReport(
        (
            InjuryUpdate(
                player=PlayerRef(name="Zay Flowers", athlete_id="4426354"),
                status="Questionable",
            ),
        )
    )

    embed = injury_embeds(report, TIME_ZONE)[0]

    assert embed.thumbnail.url is not None
    assert "w=200" in embed.thumbnail.url
    assert embed.image.url is None


def test_injury_embed_without_players_says_so() -> None:
    embed = injury_embeds(InjuryReport(), TIME_ZONE)[0]

    assert embed.description == format_no_injuries()
    assert embed.thumbnail.url == team_logo_url(RAVENS_SLUG)


def test_injury_announcement_key_changes_only_with_the_report() -> None:
    first, second = parse_injuries(_payload()).updates
    same = parse_injuries(_payload()).updates[0]
    worse = InjuryUpdate(player=second.player, status="Out", updated=second.updated)

    assert injury_announcement_key(first) == injury_announcement_key(same)
    assert injury_announcement_key(first) != injury_announcement_key(second)
    assert injury_announcement_key(second) != injury_announcement_key(worse)


def test_state_reports_whether_a_target_has_injury_history(tmp_path) -> None:
    state = AnnouncementState(str(tmp_path / "state.json"))

    assert not state.has_target_keys("injury:", "123")

    state.mark(channel_key("injury:1:Out:", "123"))

    assert state.has_target_keys("injury:", "123")
    assert not state.has_target_keys("injury:", "456")
