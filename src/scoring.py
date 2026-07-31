"""Canonical player-value scoring logic for the draft toolkit.

This is the single implementation of "how much is this player worth" used
by both the live draft API (src/recommender.py) and the offline analysis
notebooks (NB03, NB04). It replaces three separate, disagreeing
implementations that used to exist in this codebase:

  1. NB03 had its own hardcoded SQL replacement-baseline windows
     (e.g. QB posRank BETWEEN 15 AND 18) for retrospective VORP.
  2. NB04 had a second, separately hardcoded version of the same idea for
     next-season projections, feeding a regression whose output was baked
     into a static players.json.
  3. src/recommender.py had a third, roster-need-driven version that the
     live draft UI never actually called.

Replacement level here is defined as: the Nth-best player at a position,
where N = teams * starters-needed-at-that-position (+ a share of FLEX for
flex-eligible positions). That ties the baseline directly to the league's
real roster construction instead of an arbitrary rank cutoff that has to be
hand-picked and re-justified per position.
"""
from __future__ import annotations

import pandas as pd

try:
    from notebooks.config import FLEX_ELIGIBLE, ROSTER_NEEDS, TEAMS
except ImportError:  # pragma: no cover - fallback if repo root isn't on sys.path
    TEAMS = 14
    ROSTER_NEEDS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DST": 1}
    FLEX_ELIGIBLE = ("RB", "WR", "TE")

# ESPN/ADP source data spells this position two different ways across
# tables ("D/ST" in players_stats & drafts, "DST" in average_draft_position).
# That mismatch silently breaks any join/groupby keyed on position, so every
# position value should be passed through this before it's used as a key.
POSITION_ALIASES = {"D/ST": "DST", "DEF": "DST", "D-ST": "DST"}


def normalize_position(pos: str | None) -> str | None:
    if pos is None:
        return pos
    return POSITION_ALIASES.get(pos, pos)


def starters_needed(position: str, teams: int = TEAMS, roster_needs: dict | None = None) -> int:
    roster_needs = roster_needs or ROSTER_NEEDS
    base = teams * roster_needs.get(position, 0)
    if position in FLEX_ELIGIBLE:
        flex_positions = [p for p in FLEX_ELIGIBLE if roster_needs.get(p, 0) > 0]
        share = roster_needs.get("FLEX", 0) / max(len(flex_positions), 1)
        base += teams * share
    return round(base)


def compute_baselines(
    df: pd.DataFrame,
    teams: int = TEAMS,
    roster_needs: dict | None = None,
    value_col: str = "projected_points",
) -> dict:
    """Replacement-level baseline per position: the value of the last
    starter-worthy player at that position, given league size and roster
    slots.
    """
    baselines = {}
    for pos in df["position"].dropna().unique():
        n = starters_needed(pos, teams, roster_needs)
        if n <= 0:
            baselines[pos] = 0.0
            continue
        pool = df[df.position == pos].sort_values(value_col, ascending=False).head(n)
        baselines[pos] = float(pool[value_col].min()) if len(pool) else 0.0
    return baselines


def add_vorp(
    df: pd.DataFrame,
    baselines: dict,
    value_col: str = "projected_points",
    out_col: str = "vorp",
) -> pd.DataFrame:
    out = df.copy()
    out["baseline"] = out["position"].map(lambda p: baselines.get(p, 0.0))
    out[out_col] = out[value_col] - out["baseline"]
    return out


def need_weights(roster_state: dict) -> dict:
    """Players at a position the drafter still needs get weighted up."""
    return {pos: 1.0 + 0.5 * max(v["need"] - v["have"], 0) for pos, v in roster_state.items()}


def adp_pressure(league_pick_est: float, current_pick: int, next_pick: int) -> float:
    """How urgently to grab a player now vs. risk them being gone before
    your next pick, based on where the market expects them to go.
    """
    if league_pick_est <= current_pick:
        return 1.12
    if league_pick_est < next_pick:
        return 1.22
    if league_pick_est < next_pick + 12:
        return 1.05
    return 0.95

# Weight on recent-performance (avg points/game last season) in the live
# utility score. This value is informed by the NB04 walk-forward regression
# validation, which found recency to be the strongest predictor of a
# player's actual value among pre-draft-available features (see
# README methodology section) - it is not itself a fitted coefficient.
RECENCY_WEIGHT = 0.05


def score(
    df: pd.DataFrame,
    roster_state: dict,
    current_pick: int,
    next_pick: int,
    recency_col: str = "avg_last_year",
) -> pd.DataFrame:
    w = need_weights(roster_state)
    out = df.copy()
    out["pos_weight"] = out["position"].map(lambda p: w.get(p, 1.0))
    out["adp_mult"] = out["league_pick_est"].apply(lambda x: adp_pressure(x, current_pick, next_pick))
    recency = out[recency_col] if recency_col in out.columns else 0.0
    out["utility"] = (
        out["vorp"].clip(lower=0) * out["pos_weight"] * out["adp_mult"]
        + 0.02 * out["projected_points"]
        + RECENCY_WEIGHT * recency
    )
    return out.sort_values("utility", ascending=False)
