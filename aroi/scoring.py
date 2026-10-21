"""Deterministic scoring. The reviewer model only supplies 1-5 criterion scores; all arithmetic,
thresholds and grade caps live here so the ranking is reproducible and auditable."""
from __future__ import annotations

from typing import Any

RUBRIC_VERSION = "rubric-v1"

# key: (label, weight %)
CRITERIA: dict[str, tuple[str, int]] = {
    "problem_severity_and_demand": ("Problem severity and demand", 20),
    "scientific_evidence": ("Scientific evidence", 20),
    "market_fit": ("Market fit (South Africa)", 15),
    "technical_feasibility": ("Technical feasibility", 15),
    "unit_economics_and_scalability": ("Unit economics and scalability", 15),
    "competition_and_defensibility": ("Competition and defensibility", 10),
    "time_and_capital_to_market": ("Time and capital to market (5 = fast and cheap)", 5),
}
assert sum(w for _, w in CRITERIA.values()) == 100

GRADES = ["low", "medium", "high"]


def weighted_score(scores: dict[str, int]) -> float:
    """Score = sum(s_i / 5 * w_i), giving 20-100 for scores of 1-5."""
    missing = set(CRITERIA) - set(scores)
    if missing:
        raise ValueError(f"Missing criterion scores: {sorted(missing)}")
    total = 0.0
    for key, (_, weight) in CRITERIA.items():
        s = int(scores[key])
        if not 1 <= s <= 5:
            raise ValueError(f"{key} score {s} outside 1-5")
        total += s / 5 * weight
    return round(total, 2)


def action_for(score: float, grade: str, has_blocker: bool) -> str:
    """Thresholds from the blueprint, plus two guards it asked for in prose:
    a blocker or a low evidence grade can never be 'prioritize'."""
    if has_blocker:
        return "resolve_blocker_first"
    if score >= 75:
        return "prioritize_validation" if grade != "low" else "investigate_uncertainties"
    if score >= 55:
        return "investigate_uncertainties"
    return "deprioritize"


def cap_grade(model_grade: str, verified_quotes: int, web_sources: int) -> str:
    """The reviewer can't award more confidence than the stored evidence supports."""
    cap = "high"
    if verified_quotes == 0 and web_sources == 0:
        cap = "low"
    elif verified_quotes == 0 or web_sources < 3:
        cap = "medium"
    g = model_grade if model_grade in GRADES else "low"
    return GRADES[min(GRADES.index(g), GRADES.index(cap))]


def clamp01(x: Any, default: float = 0.0) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, v))


# ---------------------------------------------------------------------------
# Quant finance track: how worth re-testing is a paper's hypothesis? (never "how profitable")
# ---------------------------------------------------------------------------
FINANCE_RUBRIC_VERSION = "finance-testability-v1"


def testability_score(
    *,
    out_of_sample: bool,
    costs_modelled: bool,
    liquidity_considered: bool,
    replication: str,
    verified_quotes: int,
    evidence_confidence: float,
    red_flags: int,
) -> float:
    s = 0.0
    s += 25 if out_of_sample else 0
    s += 20 if costs_modelled else 0
    s += 10 if liquidity_considered else 0
    s += {"easy": 15, "moderate": 8}.get(replication, 0)
    s += 10 if verified_quotes > 0 else 0
    s += 20 * clamp01(evidence_confidence)
    s -= min(25, 5 * max(0, red_flags))
    return round(max(0.0, min(100.0, s)), 2)


def finance_action(score: float) -> str:
    if score >= 60:
        return "backtest_candidate"
    if score >= 40:
        return "watch"
    return "discard"
