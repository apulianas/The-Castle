"""Regenerate the bundled decision estimates, using only the Python standard library."""

from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
from pathlib import Path
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
TRAIN = (2022, 2023, 2024)
HOLDOUT = 2025
SOURCES = {
    2022: ("0c69a71eb39498956c7b1d5c1ca52ce7fe679934a95d1af249facb5ea9829ea4", "2026-02-12T10:25:24Z"),
    2023: ("4649804ee0f0a40b41e51ec75a1ce921949d7fab5459213488656b92f78560e8", "2026-02-12T10:24:52Z"),
    2024: ("23370d5d10f8104d80d46a1fc5e61f4f6f5a3263fe96fe2dd629913cfcb08c06", "2026-08-13T12:26:27Z"),
    2025: ("2f135887790a013fd004e609e37096bb4816d5cc80b9f19122e1bad478961978", "2026-08-13T12:26:09Z"),
}
# Frozen pre-calibration implementation: never import the currently shipped model.
BASELINE = {
    "conversion": [(1, .68), (2, .55), (3, .50), (4, .45), (5, .42), (6, .38),
                   (7, .35), (8, .33), (9, .31), (10, .30), (12, .26),
                   (15, .20), (20, .14), (30, .08)],
    "field_goal": [(20, .99), (25, .97), (30, .95), (35, .92), (40, .88),
                   (45, .82), (50, .72), (55, .58), (60, .40), (63, .26), (66, .15)],
    "punt": [(30, 8), (40, 10), (50, 13), (60, 19), (70, 29), (80, 39), (90, 49), (99, 58)],
    "ep": [(1, 6.3), (5, 5.8), (10, 5.2), (20, 4.3), (30, 3.7),
           (40, 3), (50, 2.3), (60, 1.9), (70, 1.5), (80, 1), (90, .5), (95, .1), (99, -.4)],
}
BASELINE["goal_conversion"] = [(x, y * .9) for x, y in BASELINE["conversion"]]

# Each observation is (predictor, outcome, game id). WP stores additional state.
Observation = tuple[float, float, str]


def number(row: dict[str, str], key: str) -> float | None:
    try:
        value = float(row.get(key, ""))
    except (ValueError, TypeError):
        return None
    return value if math.isfinite(value) else None


def interpolate(table: list, x: float) -> float:
    if x <= table[0][0]:
        return table[0][1]
    for (a, b), (c, d) in zip(table, table[1:]):
        if x <= c:
            return b + (d - b) * (x - a) / (c - a)
    return table[-1][1]


def valid(row: dict[str, str]) -> bool:
    return (
        row.get("season_type") in {"REG", "POST"}
        and row.get("game_id") not in {None, ""}
        and number(row, "play_id") is not None
        and number(row, "qtr") in {1, 2, 3, 4}
        and row.get("game_half") in {"Half1", "Half2"}
        and row.get("posteam") not in {None, ""}
        and row.get("defteam") not in {None, ""}
        and number(row, "result") is not None
        and row.get("play_type") not in {"no_play", ""}
        and number(row, "play_deleted") == 0
    )


def scrimmage(row: dict[str, str]) -> bool:
    # Sacks are pass plays and remain failures; fakes are not ordinary attempts.
    return (
        valid(row)
        and row.get("play_type") in {"run", "pass"}
        and number(row, "qb_kneel") == 0
        and number(row, "qb_spike") == 0
        and number(row, "special_teams_play") == 0
        and not any(marker in row.get("desc", "").lower()
                    for marker in ("fake", "punt formation", "field goal formation"))
        and number(row, "penalty") == 0
    )


def collect(rows: list[dict[str, str]]) -> dict[str, list]:
    samples: dict[str, list] = {k: [] for k in BASELINE}
    samples["wp"] = []
    next_score: dict[tuple[str, str], tuple[str, float]] = {}
    next_state: dict[tuple[str, str], dict[str, str]] = {}
    # Files are sorted explicitly by game/play, rather than trusting CSV row order.
    for row in reversed(rows):
        if not valid(row):
            continue
        key = (row["game_id"], row["game_half"])
        spot = number(row, "yardline_100")
        distance = number(row, "ydstogo")
        # Targets include the current scoring play, but never a subsequent half.
        if number(row, "touchdown") == 1 and row.get("td_team"):
            next_score[key] = (row["td_team"], 6.95)
        elif row.get("field_goal_result") == "made":
            next_score[key] = (row["posteam"], 3.0)
        elif number(row, "safety") == 1:
            next_score[key] = (row["defteam"], 2.0)
        if spot is None or not 1 <= spot <= 99:
            continue
        ordinary = scrimmage(row)
        if ordinary and number(row, "down") == 4 and distance is not None and distance >= 1:
            converted = number(row, "fourth_down_converted")
            failed = number(row, "fourth_down_failed")
            if converted in {0, 1} and failed in {0, 1} and converted + failed == 1:
                name = "goal_conversion" if number(row, "goal_to_go") == 1 else "conversion"
                samples[name].append((distance, converted, key[0]))
        kick = number(row, "kick_distance")
        if (row.get("play_type") == "field_goal" and number(row, "field_goal_attempt") == 1
                and number(row, "penalty") == 0 and kick is not None and 18 <= kick <= 66
                and row.get("field_goal_result") in {"made", "missed", "blocked"}):
            samples["field_goal"].append((kick, float(row["field_goal_result"] == "made"), key[0]))
        following = next_state.get(key)
        if (row.get("play_type") == "punt" and number(row, "punt_attempt") == 1
                and all(number(row, k) == 0 for k in ("punt_blocked", "fumble", "penalty", "touchdown", "safety"))
                and following is not None and following["posteam"] == row["defteam"]
                and number(following, "down") == 1):
            their_spot = number(following, "yardline_100")
            if their_spot is not None and 1 <= their_spot <= 99:
                samples["punt"].append((spot, 100 - their_spot, key[0]))
        if ordinary and number(row, "down") == 1 and distance == min(10, spot):
            half = number(row, "half_seconds_remaining")
            seconds = number(row, "game_seconds_remaining")
            margin = number(row, "score_differential")
            if half is not None and half >= 300 and margin is not None and abs(margin) <= 14:
                team, points = next_score.get(key, ("", 0.0))
                target = points if team == row["posteam"] else -points
                samples["ep"].append((spot, target, key[0]))
            if seconds is not None and 0 < seconds <= 3600 and half is not None and margin is not None:
                result = number(row, "result")
                assert result is not None
                won = .5 if result == 0 else float((result > 0) == (row["posteam"] == row["home_team"]))
                samples["wp"].append((margin, seconds, spot, half, won, key[0]))
        if row.get("play_type") in {"run", "pass", "qb_kneel", "qb_spike", "punt", "field_goal"}:
            next_state[key] = row
    for values in samples.values():
        values.reverse()
    return samples


def isotonic(values: list[float], weights: list[float], increasing: bool) -> list[float]:
    sign = 1 if increasing else -1
    blocks: list[list[float]] = []
    for index, (value, weight) in enumerate(zip(values, weights)):
        blocks.append([index, index, value * sign * weight, weight])
        while len(blocks) > 1 and blocks[-2][2] / blocks[-2][3] > blocks[-1][2] / blocks[-1][3]:
            right, left = blocks.pop(), blocks.pop()
            blocks.append([left[0], right[1], left[2] + right[2], left[3] + right[3]])
    result = [0.0] * len(values)
    for start, end, total, weight in blocks:
        for index in range(int(start), int(end) + 1):
            result[index] = sign * total / weight
    return result


def fit_curve(name: str, samples: list[Observation], prior: list) -> dict:
    nodes = (list(range(1, 31)) if "conversion" in name else
             list(range(18, 67)) if name == "field_goal" else [x for x, _ in BASELINE[name]])
    values, weights, counts = [], [], []
    prior_weight = 20 if name == "goal_conversion" else 10
    for x in nodes:
        bandwidth = (0.5 if x <= 5 else max(1, x / 4)) if "conversion" in name else 3 if name == "field_goal" else 10
        total, weight, count = 0.0, 0.0, 0
        for spot, target, _ in samples:
            share = max(0.0, 1 - abs(spot - x) / bandwidth)
            if share > 0:
                total += share * target
                weight += share
                count += 1
        values.append((total + prior_weight * interpolate(prior, x)) / (weight + prior_weight))
        weights.append(weight + prior_weight)
        counts.append(count)
    fitted = isotonic(values, weights, increasing=name == "punt")
    return {
        "curve": [[x, round(y, 8)] for x, y in zip(nodes, fitted)],
        "local_support": [[x, n] for x, n in zip(nodes, counts)],
        "prior_effective_n": prior_weight,
    }


def metrics(predictions: list[float], targets: list[float], probability: bool) -> dict:
    if not targets:
        raise ValueError("Cannot evaluate an empty sample")
    result = {"n": len(targets), "brier" if probability else "mse":
              sum((p - y) ** 2 for p, y in zip(predictions, targets)) / len(targets)}
    if probability:
        result["log_loss"] = -sum(
            y * math.log(max(.001, min(.999, p))) + (1 - y) * math.log(1 - max(.001, min(.999, p)))
            for p, y in zip(predictions, targets)
        ) / len(targets)
    return result


def beats(candidate: dict, baseline: dict) -> bool:
    return all(candidate[key] <= baseline[key] for key in baseline if key != "n")


def sigmoid(z: float) -> float:
    return max(.001, min(.999, 1 / (1 + math.exp(-max(-30, min(30, z))))))


def wp_features(row: tuple, ep: list, half_cap: bool = True) -> tuple[float, float]:
    margin, seconds, spot, half, _, _ = row
    scale = math.sqrt(max(6, seconds) / 3600)
    possession = interpolate(ep, spot) * min(1, (min(seconds, half) if half_cap else seconds) / 150)
    return margin / scale, possession / scale


def fit_wp(samples: list, ep: list) -> list[float]:
    # Two-variable Newton logistic regression, no intercept to preserve symmetry.
    data = [(wp_features(row, ep), row[4]) for row in samples]
    a, b = 1.702 / 13.5, 1.702 / 13.5
    for _ in range(30):
        ga = gb = haa = hab = hbb = 0.0
        for (x, z), target in data:
            p = sigmoid(a * x + b * z)
            variance = p * (1 - p)
            ga += (p - target) * x
            gb += (p - target) * z
            haa += variance * x * x
            hab += variance * x * z
            hbb += variance * z * z
        determinant = haa * hbb - hab * hab
        if determinant <= 0:
            raise ValueError("Singular WP fit")
        da = (hbb * ga - hab * gb) / determinant
        db = (haa * gb - hab * ga) / determinant
        a, b = a - da, b - db
        if max(abs(da), abs(db)) < 1e-9:
            break
    if not (0 < a < 1 and 0 <= b < 1):
        raise ValueError("WP coefficients outside safe monotone bounds")
    return [round(a, 10), round(b, 10)]


def calibrate(train: dict[str, list], holdout: dict[str, list]) -> dict:
    curves, reports = {}, {}
    for name in BASELINE:
        prior = BASELINE[name]
        if name == "goal_conversion":
            prior = [[x, y * .9] for x, y in reports["conversion"]["candidate_curve"]]
        fit = fit_curve(name, train[name], prior)
        probability = name not in {"ep", "punt"}
        targets = [y for _, y, _ in holdout[name]]
        baseline = metrics([interpolate(BASELINE[name], x) for x, _, _ in holdout[name]], targets, probability)
        candidate = metrics([interpolate(fit["curve"], x) for x, _, _ in holdout[name]], targets, probability)
        accepted = beats(candidate, baseline)
        curves[name] = {**fit, "selected": "historical" if accepted else "baseline",
                        "curve": fit["curve"] if accepted else BASELINE[name],
                        "train_n": len(train[name]), "holdout_n": len(holdout[name])}
        reports[name] = {"baseline": baseline, "candidate": candidate, "accepted": accepted,
                         "candidate_curve": fit["curve"]}
    candidate_ep = reports["ep"]["candidate_curve"]
    coefficients = fit_wp(train["wp"], candidate_ep)
    wp_report = {}
    for label, samples in {
        "all": holdout["wp"],
        "last_five_minutes": [row for row in holdout["wp"] if row[1] <= 300],
        "first_half_last_two_minutes": [row for row in holdout["wp"] if row[1] > 1800 and row[3] <= 120],
    }.items():
        targets = [row[4] for row in samples]
        baseline_predictions = [sigmoid(sum(wp_features(row, BASELINE["ep"], False)) * 1.702 / 13.5) for row in samples]
        candidate_predictions = [sigmoid(sum(c * x for c, x in zip(coefficients, wp_features(row, candidate_ep)))) for row in samples]
        wp_report[label] = {
            "games": len({row[5] for row in samples}),
            "baseline": metrics(baseline_predictions, targets, True),
            "candidate": metrics(candidate_predictions, targets, True),
        }
    accepted = all(beats(r["candidate"], r["baseline"]) for r in wp_report.values())
    return {
        "curves": curves,
        "wp": {"selected": "historical" if accepted else "baseline",
               "coefficients": coefficients if accepted else [1.702 / 13.5] * 2,
               "candidate_coefficients": coefficients, "half_cap": accepted,
               "possession_curve": candidate_ep if accepted else BASELINE["ep"],
               "train_n": len(train["wp"]), "holdout_n": len(holdout["wp"])},
        "evaluation": {**reports, "wp": wp_report},
    }


def read_season(path: Path) -> list[dict[str, str]]:
    with gzip.open(path, "rt", encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        required = {"game_id", "play_id", "result", "game_half", "play_type", "yardline_100",
                    "fourth_down_converted", "field_goal_result", "special_teams_play",
                    "season_type", "qtr", "posteam", "defteam", "home_team", "ydstogo",
                    "play_deleted", "qb_kneel", "qb_spike", "penalty", "down", "goal_to_go",
                    "fourth_down_failed", "kick_distance", "field_goal_attempt", "punt_attempt",
                    "punt_blocked", "fumble", "touchdown", "td_team", "safety",
                    "half_seconds_remaining", "game_seconds_remaining", "score_differential", "desc"}
        if not required.issubset(reader.fieldnames or []):
            raise ValueError(f"Missing required PBP columns in {path}")
        rows = list(reader)
    rows.sort(key=lambda row: (row["game_id"], number(row, "play_id") or 0))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache-dir", type=Path, required=True, help="Raw gzip directory outside the repository")
    parser.add_argument("--download", action="store_true", help="Fetch missing pinned release files")
    parser.add_argument("--output", type=Path, default=ROOT / "ravens_bot" / "data" / "decision_calibration.json")
    args = parser.parse_args()
    cache = args.cache_dir.resolve()
    if cache == ROOT or ROOT in cache.parents:
        parser.error("--cache-dir must be outside the repository")
    cache.mkdir(parents=True, exist_ok=True)
    train: dict[str, list] = {k: [] for k in [*BASELINE, "wp"]}
    holdout: dict[str, list] = {}
    provenance = []
    for season, (digest, updated) in SOURCES.items():
        filename = f"play_by_play_{season}.csv.gz"
        url = f"https://github.com/nflverse/nflverse-data/releases/download/pbp/{filename}"
        path = cache / filename
        if not path.exists() and args.download:
            urllib.request.urlretrieve(url, path)
        if not path.exists():
            parser.error(f"Missing {path}; supply --download or prefetch the pinned release")
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        if actual != digest:
            raise ValueError(f"Source hash mismatch for {path}; review upstream changes before repinning")
        rows = read_season(path)
        samples = collect(rows)
        provenance.append({"season": season, "url": url, "sha256": digest, "release_updated_at": updated,
                           "retrieved_utc_date": "2026-09-16", "rows": len(rows),
                           "eligible_games": len({r["game_id"] for r in rows if valid(r)}),
                           "samples": {k: len(v) for k, v in samples.items()}})
        if season in TRAIN:
            for name, values in samples.items():
                train[name].extend(values)
        else:
            holdout = samples
        print(f"{season}: {provenance[-1]['samples']}", flush=True)
    model = calibrate(train, holdout)
    artifact = {
        "schema_version": 1, "model_version": "nflverse-2022-2024-v1",
        "train_seasons": list(TRAIN), "holdout_season": HOLDOUT, "sources": provenance,
        "attribution": "Derived from nflverse/nflverse-data (nflfastR PBP), CC BY 4.0; modified by aggregation and calibration.",
        "license_url": "https://creativecommons.org/licenses/by/4.0/",
        "source_license_url": "https://github.com/nflverse/nflverse-data/blob/master/LICENSE.md",
        "filters": {
            "common": "REG+POST, regulation, known final result and teams; exclude deleted/no-play rows.",
            "conversion": "Fourth-down run/pass including sacks; exclude kneels, spikes, penalties, special_teams_play=1, and descriptions containing fake/punt formation/field goal formation (fakes can have special_teams_play=0); require converted+failed=1. Goal-to-go fitted separately.",
            "field_goal": "Actual 18-66 yard FG attempts on any down; include blocked kicks as misses; exclude penalties.",
            "punt": "Unblocked non-scoring punts without fumbles/penalties, followed by receiving-team first down in same half; actual next scrimmage spot, not gross kick distance.",
            "ep": "First-and-10 or first-and-goal, non-penalty run/pass; >=300 seconds in half, abs(score margin)<=14. Signed next score in same half: TD 6.95, FG 3, safety 2, no score 0. No nflfastR ep predictions used.",
            "wp": "First-and-10/goal non-penalty run/pass, regulation; final win=1, loss=0, tie=.5. Two logistic slopes, no teams/timeouts/spread; EP from train-only candidate curve. Candidate possession discount caps at half boundary; rejected fit retains baseline possession curve and clock behavior.",
        },
        "method": "Triangular local smoothing with baseline pseudo-observations (10; goal-to-go 20), weighted isotonic monotonicity. Fixed settings before holdout. Bandwidth: conversion 0.5 through 5 yards then max(1, distance/4); FG 3; EP/punt 10. Local support counts positive-weight observations before isotonic pooling, not independent games or effective n. Curves selected independently only if 2025 MSE, or both Brier and log loss, do not regress. WP must pass overall and both clock subgroups.",
        "limitations": [
            "Observational attempt selection, not causal fourth-down recommendation evaluation.",
            "2025 is a selection holdout, not an untouched final test; no statistical significance claim. Plays within games are correlated.",
            "Sparse long kicks/conversions and unusual punt spots rely on smoothing/prior; endpoints clamp, FG >66 excluded intentionally.",
            "EP excludes late-half and large-margin states; no down/distance after a failed conversion, return distribution, kicker, weather, team strength, timeouts or OT model.",
            "Play durations, TD/PAT value and scoring/kickoff treatment remain heuristics, not learned decision transitions.",
        ],
        **model,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(artifact, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({name: {"accepted": report.get("accepted"), "baseline": report.get("baseline"), "candidate": report.get("candidate")}
                      for name, report in model["evaluation"].items() if name != "wp"}, indent=2))
    print("WP:", json.dumps(model["wp"]), json.dumps(model["evaluation"]["wp"]))


if __name__ == "__main__":
    main()
