from __future__ import annotations

import json
import math
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from ravens_bot.calibration import MODEL, load_calibration, support_text
from ravens_bot.fourthdown import Scoreboard, conversion_rate, expected_points, field_goal_rate
from ravens_bot.winprob import possession_value, win_probability
from tools.calibrate_decisions import (
    BASELINE, beats, calibrate, collect, fit_curve, interpolate, isotonic,
    number, scrimmage, valid, wp_features,
)


ARTIFACT = Path(__file__).resolve().parents[1] / "ravens_bot" / "data" / "decision_calibration.json"


def row(**changes: str) -> dict[str, str]:
    result = {
        "game_id": "test", "play_id": "1", "season_type": "REG", "qtr": "1",
        "game_half": "Half1", "posteam": "BAL", "defteam": "CIN", "home_team": "BAL",
        "result": "7", "play_type": "run", "play_deleted": "0", "down": "4",
        "yardline_100": "50", "ydstogo": "2", "goal_to_go": "0",
        "qb_kneel": "0", "qb_spike": "0", "special_teams_play": "0", "penalty": "0",
        "fourth_down_converted": "1", "fourth_down_failed": "0", "desc": "",
        "field_goal_attempt": "0", "field_goal_result": "", "kick_distance": "",
        "punt_attempt": "0", "punt_blocked": "0", "fumble": "0",
        "touchdown": "0", "safety": "0", "half_seconds_remaining": "1200",
        "game_seconds_remaining": "3000", "score_differential": "0",
    }
    result.update(changes)
    return result


@pytest.mark.parametrize("text", ["", "NA", "NaN", "Inf", "-Infinity", "bad"])
def test_nonfinite_csv_numbers_are_missing(text: str) -> None:
    assert number({"x": text}, "x") is None
    assert collect([row(ydstogo=text)])["conversion"] == []
    assert collect([row(kick_distance=text, play_type="field_goal", field_goal_attempt="1",
                        field_goal_result="made")])["field_goal"] == []


@pytest.mark.parametrize("change", [
    {"play_type": "no_play"}, {"play_deleted": "1"}, {"qtr": "5"},
    {"season_type": "PRE"}, {"result": "NA"}, {"posteam": ""},
    {"game_half": "Overtime"}, {"play_id": "NaN"}, {"play_deleted": "NaN"},
])
def test_invalid_or_out_of_scope_rows_are_excluded(change: dict[str, str]) -> None:
    assert not valid(row(**change))
    assert all(not samples for samples in collect([row(**change)]).values())


@pytest.mark.parametrize("change", [
    {"qb_kneel": "1"}, {"qb_spike": "1"}, {"special_teams_play": "1"},
    {"desc": "Fake punt pass incomplete"}, {"penalty": "1"},
    {"desc": "(Punt formation) Direct snap to runner, 10 yards."},
    {"desc": "(Field Goal formation) Holder pass incomplete."},
])
def test_conversion_excludes_kneels_spikes_fakes_and_penalties(change: dict[str, str]) -> None:
    assert not scrimmage(row(**change))
    assert collect([row(**change)])["conversion"] == []


def test_sacks_remain_conversion_failures_and_goal_to_go_is_separate() -> None:
    samples = collect([
        row(play_type="pass", sack="1", fourth_down_converted="0", fourth_down_failed="1"),
        row(play_id="2", goal_to_go="1", yardline_100="2"),
    ])
    assert samples["conversion"] == [(2, 0, "test")]
    assert samples["goal_conversion"] == [(2, 1, "test")]


def test_blocked_field_goals_are_misses_and_long_attempts_are_not_extrapolated() -> None:
    samples = collect([
        row(play_type="field_goal", field_goal_attempt="1", field_goal_result="blocked",
            kick_distance="60", down="2"),
        row(play_type="field_goal", field_goal_attempt="1", field_goal_result="made",
            kick_distance="67", play_id="2"),
    ])
    assert samples["field_goal"] == [(60, 0, "test")]
    assert field_goal_rate(67) == 0
    assert 0 < field_goal_rate(66) < .5
    assert "sparse/prior-sensitive" in support_text("field_goal", 66)


def test_punts_use_receivers_actual_next_spot_not_gross_kick_distance() -> None:
    punt = row(play_type="punt", punt_attempt="1", kick_distance="55")
    receive = row(play_id="2", posteam="CIN", defteam="BAL", down="1",
                  ydstogo="10", yardline_100="85")
    assert collect([punt, receive])["punt"] == [(50, 15, "test")]
    for change in [{"fumble": "1"}, {"punt_blocked": "1"}, {"penalty": "1"}, {"touchdown": "1"}]:
        assert collect([{**punt, **change}, receive])["punt"] == []
    assert collect([punt, {**receive, "game_half": "Half2"}])["punt"] == []
    assert collect([punt, {**receive, "posteam": "BAL"}])["punt"] == []


def test_next_score_targets_respect_halves_and_team_perspective() -> None:
    first = row(down="1", ydstogo="10")
    no_play = row(play_id="2", play_type="no_play", touchdown="1", td_team="BAL")
    fg = row(play_id="3", posteam="CIN", defteam="BAL",
             play_type="field_goal", field_goal_result="made", field_goal_attempt="1", kick_distance="40")
    samples = collect([first, no_play, fg])
    assert samples["ep"] == [(50, -3, "test")]
    assert collect([first, {**fg, "game_half": "Half2", "qtr": "3"}])["ep"] == [(50, 0, "test")]
    assert collect([first, {**fg, "game_id": "other"}])["ep"] == [(50, 0, "test")]
    assert collect([first, row(play_id="2", touchdown="1", td_team="BAL")])["ep"] == [(50, 6.95, "test")]
    assert collect([first, row(play_id="2", safety="1")])["ep"] == [(50, -2, "test")]


def test_ep_excludes_late_half_blowouts_and_nonstandard_first_down_distance() -> None:
    first = row(down="1", ydstogo="10")
    for changes in [{"half_seconds_remaining": "299"}, {"score_differential": "15"}, {"ydstogo": "15"}]:
        assert collect([{**first, **changes}])["ep"] == []
    assert collect([row(down="1", yardline_100="5", ydstogo="5")])["ep"]


def test_wp_features_use_only_preplay_inputs_and_final_result_only_labels() -> None:
    original = (3, 1200, 30, 1200, 1, "game")
    assert wp_features(original, BASELINE["ep"]) == wp_features((*original[:4], 0, "different"), BASELINE["ep"])
    first = row(down="1", ydstogo="10")
    win = collect([first])["wp"][0]
    loss = collect([{**first, "result": "-14"}])["wp"][0]
    assert win[:4] == loss[:4]
    assert (win[4], loss[4]) == (1, 0)


def test_smoothing_is_monotone_deterministic_and_shrinks_empty_tails() -> None:
    observations = [(1, 1, "a"), (2, 0, "a"), (3, 1, "b")]
    fit = fit_curve("conversion", observations, BASELINE["conversion"])
    assert fit == fit_curve("conversion", observations, BASELINE["conversion"])
    values = [y for _, y in fit["curve"]]
    assert all(0 < y < 1 for y in values)
    assert values == sorted(values, reverse=True)
    assert fit["local_support"][-1] == [30, 0]
    assert isotonic([3, 1, 2], [1, 1, 1], True) == [2, 2, 2]
    assert not beats({"n": 10, "brier": .1, "log_loss": .4}, {"n": 10, "brier": .2, "log_loss": .3})


def test_holdout_outcomes_cannot_change_fitted_curves_or_wp_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    train = {name: [(x, y, "train") for x, y in table] for name, table in BASELINE.items()}
    train["wp"] = [(1, 2400, 50, 600, 1, "train")]
    holdout = {name: [(x, y, "held") for x, y in table] for name, table in BASELINE.items()}
    holdout["wp"] = [(1, 2400, 50, 600, 1, "held"), (1, 100, 50, 100, 1, "held"),
                     (1, 1850, 50, 50, 1, "held")]
    fit_inputs = []

    def fit(samples: list, ep: list) -> list:
        fit_inputs.append((samples, ep))
        return [.1, .1]

    monkeypatch.setattr("tools.calibrate_decisions.fit_wp", fit)
    original = calibrate(train, holdout)
    flipped = {name: [(x, -y if name in {"ep", "punt"} else 1-y, game) for x, y, game in holdout[name]]
               for name in BASELINE}
    flipped["wp"] = [(*sample[:4], 0, sample[5]) for sample in holdout["wp"]]
    changed = calibrate(train, flipped)
    assert fit_inputs[0] == fit_inputs[1]
    for name in BASELINE:
        assert original["evaluation"][name]["candidate_curve"] == changed["evaluation"][name]["candidate_curve"]


def test_bundled_artifact_support_and_holdout_selection_are_consistent() -> None:
    artifact = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    assert artifact["train_seasons"] == [2022, 2023, 2024]
    assert artifact["holdout_season"] == 2025
    assert MODEL.curves["conversion"].train_n == 2100
    assert MODEL.curves["goal_conversion"].train_n == 204
    assert MODEL.curves["field_goal"].train_n == 3326
    assert MODEL.curves["punt"].train_n == 5916
    assert MODEL.curves["ep"].train_n == 29691
    for name, curve in MODEL.curves.items():
        assert curve.historical
        report = artifact["evaluation"][name]
        assert beats(report["candidate"], report["baseline"])
    assert not MODEL.wp_historical
    assert not MODEL.wp_half_cap
    report = artifact["evaluation"]["wp"]["first_half_last_two_minutes"]
    assert not beats(report["candidate"], report["baseline"])
    assert conversion_rate(1) == pytest.approx(.70543735)
    assert field_goal_rate(60) == pytest.approx(.51677852)
    assert expected_points(50) == pytest.approx(2.7294897)


def test_rejected_wp_fit_preserves_evaluated_baseline_including_end_half() -> None:
    assert MODEL.wp_possession_curve == tuple((x, float(y)) for x, y in BASELINE["ep"])
    for seconds in (1, 6, 100, 900, 1850, 3600):
        for margin in (-14, -3, 0, 3, 14):
            points = possession_value(interpolate(BASELINE["ep"], 50), seconds)
            z = 1.702 * (margin + points) / (13.5 * math.sqrt(max(seconds, 6) / 3600))
            expected = max(.001, min(.999, 1 / (1 + math.exp(-z))))
            assert win_probability(margin, seconds, points) == pytest.approx(expected)
            assert Scoreboard(margin, seconds, 5).keeping_ball(50) == pytest.approx(expected)


@pytest.mark.parametrize("mutation", ["nonfinite", "unordered", "nonmonotone", "schema"])
def test_bad_bundled_artifact_fails_explicitly(tmp_path: Path, mutation: str) -> None:
    artifact = json.loads(ARTIFACT.read_text(encoding="utf-8"))
    points = artifact["curves"]["conversion"]["curve"]
    if mutation == "nonfinite":
        points[0][1] = float("nan")
    elif mutation == "unordered":
        points[1][0] = points[0][0]
    elif mutation == "nonmonotone":
        points[1][1] = 1.0
    else:
        artifact["schema_version"] = 2
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(artifact), encoding="utf-8")
    with pytest.raises(ValueError):
        load_calibration(path)


def test_package_only_copy_loads_artifact_without_repo_or_network(tmp_path: Path) -> None:
    shutil.copytree(ARTIFACT.parents[1], tmp_path / "ravens_bot",
                    ignore=shutil.ignore_patterns("__pycache__"))
    result = subprocess.run(
        [sys.executable, "-I", "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); "
         "from ravens_bot.fourthdown import field_goal_rate; "
         "assert 0.5 < field_goal_rate(60) < 0.6", str(tmp_path)],
        cwd=tmp_path, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
