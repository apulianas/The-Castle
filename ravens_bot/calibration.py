"""Validated, bundled historical estimates. No network or fitting at runtime."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import NamedTuple


class Curve(NamedTuple):
    points: tuple[tuple[int, float], ...]
    support: tuple[tuple[int, int], ...]
    train_n: int
    historical: bool

    def nearby_samples(self, value: float) -> int:
        return min(self.support, key=lambda point: abs(point[0] - value))[1]


class Calibration(NamedTuple):
    version: str
    curves: dict[str, Curve]
    wp_coefficients: tuple[float, float]
    wp_possession_curve: tuple[tuple[int, float], ...]
    wp_historical: bool
    wp_half_cap: bool


def _points(raw: object, lower: float, upper: float) -> tuple[tuple[int, float], ...]:
    if not isinstance(raw, list) or len(raw) < 2:
        raise ValueError("Calibration curve must contain at least two points")
    points = []
    for pair in raw:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError("Invalid calibration point")
        x, y = pair
        if type(x) is not int or not isinstance(y, (int, float)) or not math.isfinite(y):
            raise ValueError("Nonfinite or invalid calibration point")
        if not lower <= y <= upper or (points and x <= points[-1][0]):
            raise ValueError("Unordered or out-of-range calibration curve")
        points.append((x, float(y)))
    return tuple(points)


def load_calibration(path: Path) -> Calibration:
    # Corrupt/missing artifacts fail explicitly; never silently revert to constants.
    raw = json.loads(path.read_text(encoding="utf-8"))
    if raw["schema_version"] != 1:
        raise ValueError("Unsupported decision calibration schema")
    curves = {}
    for name in ("conversion", "goal_conversion", "field_goal", "punt", "ep"):
        entry = raw["curves"][name]
        bounds = (1, 99) if name == "punt" else (-6.95, 6.95) if name == "ep" else (0, 1)
        points = _points(entry["curve"], *bounds)
        if any((b[1] < a[1] if name == "punt" else b[1] > a[1]) for a, b in zip(points, points[1:])):
            raise ValueError(f"Nonmonotone calibration curve: {name}")
        support = _points(entry["local_support"], 0, 1_000_000)
        if any(not count.is_integer() for _, count in support) or entry["train_n"] <= 0:
            raise ValueError(f"Invalid sample counts: {name}")
        if entry["selected"] not in {"historical", "baseline"}:
            raise ValueError(f"Unknown curve selection: {name}")
        curves[name] = Curve(points, tuple((x, int(n)) for x, n in support),
                             entry["train_n"], entry["selected"] == "historical")
    wp = raw["wp"]
    a, b = wp["coefficients"]
    if not all(isinstance(c, (float, int)) and math.isfinite(c) and 0 < c < 1 for c in (a, b)):
        raise ValueError("Invalid WP coefficients")
    if wp["selected"] not in {"historical", "baseline"} or type(wp["half_cap"]) is not bool:
        raise ValueError("Invalid WP selection")
    return Calibration(raw["model_version"], curves, (a, b),
                       _points(wp["possession_curve"], -6.95, 6.95),
                       wp["selected"] == "historical", wp["half_cap"])


MODEL = load_calibration(Path(__file__).with_name("data") / "decision_calibration.json")
MODEL_SOURCE = "nflverse 2022-2024; 2025 holdout"
MODEL_LIMITS = (
    f"{MODEL_SOURCE}. Smoothed league averages; sparse tails use priors. "
    "No team strength, timeouts, kicker or weather adjustment."
)
WP_DESCRIPTION = (
    "Historically fitted regulation WP."
    if MODEL.wp_historical
    else "WP remains a score/clock approximation: historical fit failed the late-first-half holdout gate."
)


def support_text(name: str, value: float) -> str:
    curve = MODEL.curves[name]
    count = curve.nearby_samples(value)
    source = "historical" if curve.historical else "baseline; candidate"
    tail = "; sparse/prior-sensitive" if count < 100 else ""
    if value < curve.points[0][0] or value > curve.points[-1][0]:
        tail += "; endpoint estimate"
    return f"{source} nearby n={count}{tail}"
