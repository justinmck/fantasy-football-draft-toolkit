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

import ast
import json
import math

import numpy as np
import pandas as pd

try:
    from notebooks.config import FLEX_ELIGIBLE, ROSTER_NEEDS, TEAMS
except ImportError:  # pragma: no cover - fallback if repo root isn't on sys.path
    TEAMS = 14
    ROSTER_NEEDS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DST": 1}
    FLEX_ELIGIBLE = ("RB", "WR", "TE")

# ADP sentinel used when a player has no market data at all (see
# recommender.load_candidates' COALESCE). Anything at or above this is
# "unknown", not "goes at pick 999", and must not be read as a real estimate.
NO_ADP_SENTINEL = 900.0

# ESPN/ADP source data spells this position two different ways across
# tables ("D/ST" in players_stats & drafts, "DST" in average_draft_position).
# That mismatch silently breaks any join/groupby keyed on position, so every
# position value should be passed through this before it's used as a key.
POSITION_ALIASES = {"D/ST": "DST", "DEF": "DST", "D-ST": "DST"}


def normalize_position(pos: str | None) -> str | None:
    if pos is None:
        return pos
    return POSITION_ALIASES.get(pos, pos)


# Slot names that identify exactly one position. Order matters only in that
# every entry is unambiguous; FLEX-style slots are deliberately absent.
_SINGULAR_POSITION_SLOTS = ("QB", "RB", "WR", "TE", "K")

# ESPN reports eligible slots as names in its exported CSVs but as numeric ids
# in the live API, and both reach this function. Only the unambiguous ones are
# listed - flex slots (3, 5, 23) identify no single position, and bench (20) /
# IR (21) identify none at all.
_SLOT_ID_NAMES = {0: "QB", 2: "RB", 4: "WR", 6: "TE", 16: "D/ST", 17: "K"}


def position_from_eligible_slots(slots):
    """Derive a player's real position from ESPN's `eligibleSlots` list.

    ESPN's player objects expose `lineupSlot` (where the player sat in a
    roster *this* week - "BE" for bench, "RB/WR/TE" for a flex start, or a
    raw slot id) and `eligibleSlots` (every roster slot the player is
    *allowed* to fill, which is a property of the player, not of any given
    week). Only the latter identifies position.

    This project originally recorded `lineupSlot` into the `position` column,
    which meant ~76% of every season's rows landed in `players_stats` labelled
    "0" or "BE" and were then silently dropped by every downstream
    `position.isin(POSITIONS)` filter. Deriving from `eligibleSlots` instead
    recovers 99.97% of rows and agrees with the existing labels on 100% of
    the rows that were already correct.

    Accepts a list, a JSON string (as stored in the DB), or a Python-repr
    string (as stored in the raw CSVs). Returns None when no fantasy position
    is present - e.g. punters, whose only eligible slot is "P".
    """
    if slots is None:
        return None
    if isinstance(slots, str):
        text_value = slots.strip()
        if not text_value:
            return None
        try:
            slots = json.loads(text_value)
        except (ValueError, TypeError):
            try:
                slots = ast.literal_eval(text_value)
            except (ValueError, SyntaxError):
                return None
    if not isinstance(slots, (list, tuple, set)):
        return None

    available = set()
    for slot in slots:
        # An int (or a digit string) is a slot id; anything else is a name.
        try:
            available.add(_SLOT_ID_NAMES[int(slot)])
            continue
        except (TypeError, ValueError, KeyError):
            pass
        available.add(str(slot).strip())
    for position in _SINGULAR_POSITION_SLOTS:
        if position in available:
            return position
    # Defense is the one position ESPN spells differently from our POSITIONS
    # constant, so normalize it here rather than leaving it to callers.
    if "D/ST" in available or "DST" in available:
        return "DST"
    return None


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


def position_spread(
    df: pd.DataFrame,
    teams: int = TEAMS,
    roster_needs: dict | None = None,
    vorp_col: str = "vorp",
) -> pd.Series:
    """Standard deviation of VORP among the startable pool at each position
    (the top `starters_needed` players by VORP, i.e. the players who'd
    actually start given this league's roster slots).

    Positions with a steep replacement-level "cliff" (most notably QB, where
    only 1 starts per team but the gap between QB1 and the QB baseline is
    huge) end up with a much wider spread here than positions like RB/WR
    that have several startable slots. That's the raw-VORP scale mismatch
    add_vorp_z() corrects for - not a sign that compute_baselines() itself
    is wrong.
    """
    spreads = {}
    for pos in df["position"].dropna().unique():
        n = max(starters_needed(pos, teams, roster_needs), 1)
        pool = df[df.position == pos].sort_values(vorp_col, ascending=False).head(n)
        spreads[pos] = float(pool[vorp_col].std(ddof=0)) if len(pool) > 1 else 0.0
    return pd.Series(spreads)


def _reference_spread(spreads: pd.Series) -> float:
    """Anchor scale = average spread across the FLEX-eligible positions
    (RB/WR/TE), since those positions compete for the same roster slots
    (2 RB/WR/TE starters + FLEX) and are the fairest common yardstick -
    unlike QB/K/DST, which only ever compare against themselves for a
    single slot.
    """
    ref = spreads.reindex([p for p in FLEX_ELIGIBLE if p in spreads.index]).dropna()
    if len(ref) == 0:
        return float(spreads.mean()) if len(spreads) else 1.0
    return float(ref.mean())


def add_vorp_z(
    df: pd.DataFrame,
    teams: int = TEAMS,
    roster_needs: dict | None = None,
    vorp_col: str = "vorp",
    out_col: str = "vorp_z",
) -> pd.DataFrame:
    """Rescale VORP by position spread relative to the FLEX-eligible
    reference spread, so positions aren't ranked purely by how steep their
    raw-points replacement cliff happens to be.

    scale = min(reference_spread / this_position's_spread, 1.0)

    Capping the multiplier at 1.0 means reference positions (RB/WR/TE) are
    left essentially unchanged, and only high-spread positions (like QB) get
    dampened - a plain z-score would instead shrink every position's VORP to
    a similarly tiny scale and let minor terms (e.g. the small
    projected_points bonus in score()) swamp the ranking instead.

    This is purely additive: `vorp` (the old, undampened calculation) is
    left untouched on the input df so old and new can be compared directly.
    """
    out = df.copy()
    spreads = position_spread(out, teams, roster_needs, vorp_col=vorp_col)
    ref = _reference_spread(spreads)

    def scale(pos):
        s = spreads.get(pos)
        if not s or s <= 0:
            return 1.0
        return min(ref / s, 1.0)

    out[out_col] = out[vorp_col] * out["position"].map(scale)
    return out


# ---------------------------------------------------------------------------
# Roster need
# ---------------------------------------------------------------------------
#
# NB05's ADP benchmark found that the projections barely out-rank the market at
# the top of the board, and NB04's ablation found no pre-season feature improves
# on the projection itself. So the edge this tool can actually offer isn't a
# better forecast - it's the three things a projection contains no information
# about at all: which slots *you* still need to fill, how long a player will
# still be there, and how uncertain the number is. The functions below are
# where those three live.

NEED_WEIGHT = 0.5  # value inflation per open starting slot at a position

# Bench depth is worth less than an unfilled starting slot, and deliberately so:
# an empty starting slot is a guaranteed zero every week, while a bench player
# only pays off in the weeks your starter is out. Capped below NEED_WEIGHT so a
# backup can never outrank filling a hole in the lineup.
BENCH_WEIGHT = 0.25

# How often a *startable* player at each position actually missed games, measured
# over 2020-2025 on the top `starters_needed(pos)` players by season points - the
# same replacement-level tier the VORP baseline uses. This is the honest driver
# of how much a bench spot at a position is worth: you only ever need a backup in
# the weeks your starter isn't playing.
#
#   RB 0.102   TE 0.086   WR 0.076   QB 0.043   K 0.023   DST 0.012
#
# The ordering is the whole point. Kickers and defenses essentially never miss
# time, so a second one is a wasted roster spot - which is exactly what the board
# used to get wrong, treating a backup K identically to a third RB once the
# starting lineup was full. Recomputed by `notebooks/compute_availability.py`
# into the `position_availability` table; these defaults keep the live tool
# working on a database where that hasn't been run.
DEFAULT_MISSED_GAME_RATE = {
    "RB": 0.102, "TE": 0.086, "WR": 0.076, "QB": 0.043, "K": 0.023, "DST": 0.012,
}


def depth_needs(
    teams: int = TEAMS,
    roster_needs: dict | None = None,
    missed_game_rate: dict | None = None,
) -> dict:
    """Relative value of a *bench* spot at each position, normalised to [0, 1].

    Two things decide how much depth a position is worth, and both are read from
    data rather than picked:

    - **How often its starters miss time** (`missed_game_rate`), i.e. how likely
      you are to need the backup at all.
    - **How many of them you start.** Starting two RBs plus a share of FLEX means
      roughly 2.4x the injury exposure of a single QB, so the same per-player
      miss rate implies far more depth need.

    Multiplying the two and normalising so the highest position sits at 1.0
    gives, for a standard 14-team roster: RB 1.00, WR 0.74, TE 0.49, QB 0.18,
    K 0.10, DST 0.05. A backup RB is worth roughly twenty times a backup DST,
    which matches how drafts are actually played.
    """
    roster_needs = roster_needs or ROSTER_NEEDS
    rates = missed_game_rate or DEFAULT_MISSED_GAME_RATE
    raw = {}
    for pos in rates:
        # starters_needed() already folds in this position's share of FLEX.
        exposure = starters_needed(pos, teams=teams, roster_needs=roster_needs) / max(teams, 1)
        raw[pos] = exposure * rates[pos]
    peak = max(raw.values(), default=0.0)
    if peak <= 0:
        return {pos: 0.0 for pos in raw}
    return {pos: v / peak for pos, v in raw.items()}


def bench_weights(
    depth: dict | None,
    bench_remaining: int | None,
    teams: int = TEAMS,
    roster_needs: dict | None = None,
    missed_game_rate: dict | None = None,
) -> dict:
    """Multiplier contribution from bench spots, per position.

    Applies only once there's actually bench room left: `bench_remaining <= 0`
    returns nothing, so a drafter with no spare picks is never told to take a
    backup over a starter.

    Diminishing per extra body at a position (`1 / (1 + already_held)`) because
    the first backup RB covers most of the injury risk and the fourth covers
    very little - without it the board would happily stack six running backs.
    """
    if not bench_remaining or bench_remaining <= 0:
        return {}
    depth = depth or {}
    needs = depth_needs(teams=teams, roster_needs=roster_needs, missed_game_rate=missed_game_rate)
    return {
        pos: BENCH_WEIGHT * need / (1.0 + depth.get(pos, 0))
        for pos, need in needs.items()
    }


def open_slots(roster_state: dict, flex_eligible=FLEX_ELIGIBLE) -> dict:
    """Open starting slots per position, including a share of any unfilled
    FLEX slot spread across the FLEX-eligible positions.

    A FLEX slot is a real, draftable need, but it doesn't belong to any single
    position - it's satisfied by whichever of RB/WR/TE you take next. Splitting
    it evenly is the honest representation: each eligible position gets a
    partial claim rather than the slot being ignored (as it was previously,
    since no player's `position` is ever literally "FLEX") or triple-counted.
    """
    direct = {
        pos: max(v.get("need", 0) - v.get("have", 0), 0)
        for pos, v in roster_state.items()
        if pos != "FLEX"
    }
    flex = roster_state.get("FLEX", {})
    flex_open = max(flex.get("need", 0) - flex.get("have", 0), 0)
    if flex_open:
        eligible = [p for p in flex_eligible if p in direct]
        if eligible:
            share = flex_open / len(eligible)
            for pos in eligible:
                direct[pos] += share
    return direct


def roster_urgency(roster_state: dict, picks_remaining: int | None) -> float:
    """How hard to lean on need, given how many picks you have left.

    Early in a draft an open slot is barely a constraint - there are plenty of
    picks left to fill it, so you can take the best player available. Once
    picks remaining approaches the number of slots still open, every remaining
    pick has to fill a hole, and need should dominate raw value. This returns
    1.0 in the roomy case and rises smoothly toward 2.0 as the two converge.

    `picks_remaining=None` (caller didn't say) returns 1.0, which reduces this
    to the original need-only behaviour.
    """
    if not picks_remaining or picks_remaining <= 0:
        return 1.0
    total_open = sum(open_slots(roster_state).values())
    if total_open <= 0:
        return 1.0
    # ratio > 1 means there aren't enough picks left to fill every open slot.
    ratio = total_open / picks_remaining
    return 1.0 + min(max(ratio, 0.0), 1.0)


def need_weights(
    roster_state: dict,
    picks_remaining: int | None = None,
    depth: dict | None = None,
    bench_remaining: int | None = None,
    teams: int = TEAMS,
    missed_game_rate: dict | None = None,
) -> dict:
    """Multiplier per position for how badly the drafter still needs it.

    weight = 1 + NEED_WEIGHT * open_starting_slots * urgency
               + BENCH_WEIGHT * depth_need / (1 + already_held)

    The second term is the bench. Without it every position collapsed to exactly
    1.0 the moment the starting lineup was full, so with (say) seven picks still
    to make the board stopped distinguishing between a backup RB and a second
    kicker - it rated them identically, which is badly wrong in a 16-round
    draft where over a third of the picks are bench.

    Leaving `depth`/`bench_remaining` unset omits the bench term entirely, which
    reproduces the previous starters-only behaviour exactly.
    """
    urgency = roster_urgency(roster_state, picks_remaining)
    opens = open_slots(roster_state)
    bench = bench_weights(
        depth, bench_remaining, teams=teams, missed_game_rate=missed_game_rate
    )
    positions = set(opens) | set(bench)
    return {
        pos: 1.0 + NEED_WEIGHT * opens.get(pos, 0.0) * urgency + bench.get(pos, 0.0)
        for pos in positions
    }


# ---------------------------------------------------------------------------
# Pick timing
# ---------------------------------------------------------------------------

PRESSURE_WEIGHT = 0.25  # max value inflation for a player certain to be gone
ADP_SIGMA_FLOOR = 6.0   # picks; nobody's draft slot is knowable tighter than this
ADP_SIGMA_FRACTION = 0.22  # uncertainty grows proportionally deeper into the draft


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def availability(league_pick_est: float, next_pick: int) -> float:
    """Probability the player is still on the board at your *next* turn.

    Treats the pick a player actually goes at as Normal(league_pick_est, sigma)
    rather than a certainty. sigma widens with draft depth because pick 8 is far
    more predictable than pick 140 - the crowd agrees closely on elite players
    and barely at all on depth pieces.

    Returns 1.0 for players with no market data (the 999 ADP sentinel): unknown
    is not the same as "will last forever", but assuming they're available is
    the neutral choice, since we have no evidence they'll be taken.
    """
    if league_pick_est is None or not np.isfinite(league_pick_est):
        return 1.0
    if league_pick_est >= NO_ADP_SENTINEL:
        return 1.0
    sigma = max(ADP_SIGMA_FLOOR, ADP_SIGMA_FRACTION * league_pick_est)
    return 1.0 - _norm_cdf((next_pick - league_pick_est) / sigma)


def availability_pressure(
    league_pick_est: float,
    current_pick: int,
    next_pick: int,
    weight: float = PRESSURE_WEIGHT,
) -> float:
    """Smooth replacement for `adp_pressure`: inflate a player's value in
    proportion to how unlikely they are to survive until your next turn.

    The old step function had two problems. It was a cliff - a player estimated
    one pick either side of a threshold got a 17% different multiplier for no
    real reason - and it was degenerate exactly when it mattered, because the
    UI passed `next_pick == current_pick` on your own turn, collapsing the
    "should I wait?" comparison the function exists to make.

    Here, pressure runs from 1.0 (certain to still be there, so no reason to
    reach) to 1 + weight (certain to be gone, so it's now or never).
    """
    if next_pick <= current_pick:
        # No later turn to compare against - waiting isn't an option, so
        # there's nothing to trade off and every player is treated equally.
        return 1.0
    return 1.0 + weight * (1.0 - availability(league_pick_est, next_pick))


def adp_pressure(league_pick_est: float, current_pick: int, next_pick: int) -> float:
    """Original step-function pick pressure, kept for comparison.

    Superseded by `availability_pressure` (which `score()` now uses by
    default), but retained unchanged - same as `vorp` alongside `vorp_z` - so
    the old and new timing behaviour can be compared rather than one silently
    replacing the other. Pass `pressure_fn=adp_pressure` to `score()` to get
    the previous ranking back.
    """
    if league_pick_est <= current_pick:
        return 1.12
    if league_pick_est < next_pick:
        return 1.22
    if league_pick_est < next_pick + 12:
        return 1.05
    return 0.95


# ---------------------------------------------------------------------------
# Risk / confidence
# ---------------------------------------------------------------------------

RISK_AVERSION = 0.20  # most that uncertainty can discount a player's value

# Uncertainty comes from two sources that measure genuinely different things,
# so both are needed:
#
#   model interval (NB04)      - how much a Ridge fit wobbles across bootstrap
#                                resamples. Narrow (a few %). Distinguishes
#                                players *within* a position.
#   position reliability (NB05) - R^2 of projected vs. actual VORP by position,
#                                measured over 2020-2025. Wide (QB 0.52, WR
#                                0.37, K -0.03, DST 0.00). Distinguishes
#                                *between* positions.
#
# Weighting the position term higher is deliberate: it measures outcome
# uncertainty (how wrong the projection actually turns out to be), which is what
# a drafter is exposed to. The model interval only measures parameter
# uncertainty, and on its own is too narrow to reorder anything - measured at
# roughly a 3% spread across the top 40, which moved one adjacent pair.
MODEL_INTERVAL_WEIGHT = 0.35
POSITION_RELIABILITY_WEIGHT = 0.65


def model_confidence(df: pd.DataFrame) -> pd.Series:
    """How much of a player's projected value survives the pessimistic case.

    NB04 bootstraps its model (300 resamples) and reports a 90% interval per
    player. `ci_low / predicted_vorp` is the share of the point estimate that
    holds up at the 5th percentile: 1.0 means the downside is barely worse than
    the expectation, 0.0 means the floor is zero value.

    Deliberately a *ratio*, not the raw interval width: width scales with the
    size of the projection, so an absolute penalty would just re-penalise good
    players. The ratio is scale-free and applies to any value column.

    Returns 1.0 (neutral) where the intervals aren't available, or where the
    point estimate is at/below replacement level and the ratio is meaningless.
    """
    if not {"predicted_vorp", "ci_low"} <= set(df.columns):
        return pd.Series(1.0, index=df.index)
    point = pd.to_numeric(df["predicted_vorp"], errors="coerce")
    low = pd.to_numeric(df["ci_low"], errors="coerce")
    return (low / point.where(point > 0)).clip(0.0, 1.0).fillna(1.0)


# A rookie's projection has no prior-season production behind it, and NB05
# measures the cost: R^2 0.282 for rookies against 0.463 for veterans over
# 2021-2025, i.e. a rookie projection is worth roughly 61% of a veteran's at the
# same position. Used as a fallback when the `rookie_reliability` table hasn't
# been joined; the measured value from NB05 takes precedence when present.
DEFAULT_ROOKIE_RELIABILITY_FACTOR = 0.61


def position_reliability(df: pd.DataFrame) -> pd.Series:
    """Historical share of variance in actual VORP that the projection at this
    position has explained (NB05's by-position R^2, floored at 0), scaled down
    for rookies.

    A kicker's projection has an R^2 of about -0.03 across six seasons: it
    carries no information about what that kicker will actually deliver. A QB's
    is 0.52. Ranking both as though the number in front of you means the same
    thing is the mistake this corrects.

    Position and rookie status are separate axes and compose multiplicatively -
    a rookie TE and a rookie RB shouldn't collapse to the same number. Note the
    rookie adjustment is about *reliability*, not quality: NB05 finds rookies
    out-deliver their projections by 27 VORP on average, so they aren't worse
    players, just far less predictable ones.

    Returns 1.0 (neutral) when neither table has been joined, so the live tool
    still works before NB05 has ever been run.
    """
    if "reliability" in df.columns:
        base = pd.to_numeric(df["reliability"], errors="coerce").clip(0.0, 1.0).fillna(1.0)
    else:
        base = pd.Series(1.0, index=df.index)

    if "is_rookie" not in df.columns:
        return base

    rookie = pd.to_numeric(df["is_rookie"], errors="coerce").fillna(0) > 0
    if "rookie_factor" in df.columns:
        factor = (
            pd.to_numeric(df["rookie_factor"], errors="coerce")
            .clip(0.0, 1.0)
            .fillna(DEFAULT_ROOKIE_RELIABILITY_FACTOR)
        )
    else:
        factor = pd.Series(DEFAULT_ROOKIE_RELIABILITY_FACTOR, index=df.index)

    # Veterans keep their position's reliability unchanged; only rookies scale.
    multiplier = pd.Series(1.0, index=df.index).where(~rookie, factor)
    return (base * multiplier).clip(0.0, 1.0)


def confidence(df: pd.DataFrame) -> pd.Series:
    """Blended confidence in [0, 1]: how much to trust this player's number.

    Combines within-position model uncertainty with historical reliability by
    position and rookie status (see the weight constants above for why both).
    """
    return (
        MODEL_INTERVAL_WEIGHT * model_confidence(df)
        + POSITION_RELIABILITY_WEIGHT * position_reliability(df)
    )


def risk_multiplier(df: pd.DataFrame, risk_aversion: float = RISK_AVERSION) -> pd.Series:
    """Turn confidence into a value multiplier in [1 - risk_aversion, 1].

    Capped deliberately: this is meant to break ties between players the board
    already rates similarly - "take the safer one when it's close" - not to
    override a genuine value gap. At the default 0.20, a player has to be within
    ~20% of another before uncertainty can reorder them.

    `risk_aversion=0` disables the term entirely, which is how the API exposes a
    "ignore uncertainty" setting and how the ablation in the tests isolates it.
    """
    return (1.0 - risk_aversion) + risk_aversion * confidence(df)


# Weight on recent-performance (avg points/game last season) in the live
# utility score.
#
# NOTE: this was previously described as validated by NB04. It isn't. NB04's
# feature ablation (added when the position-column fix quadrupled the sample)
# found that adding `avg_last_year` to `proj_vorp` moves holdout R^2 by -0.002
# - i.e. recency adds no measurable predictive value. The weight is kept small
# and deliberately, as a draft-day tiebreaker between players whose projections
# are otherwise indistinguishable, but it is a preference, not a finding.
RECENCY_WEIGHT = 0.05

PROJECTED_POINTS_WEIGHT = 0.02  # small tiebreaker so equal-VORP players still order


def score(
    df: pd.DataFrame,
    roster_state: dict,
    current_pick: int,
    next_pick: int,
    recency_col: str = "avg_last_year",
    vorp_col: str = "vorp_z",
    picks_remaining: int | None = None,
    pressure_fn=availability_pressure,
    risk_aversion: float = RISK_AVERSION,
    depth: dict | None = None,
    bench_remaining: int | None = None,
    teams: int = TEAMS,
    missed_game_rate: dict | None = None,
) -> pd.DataFrame:
    """Rank candidates by draft-pick utility.

        utility = max(value, 0) * need * timing * confidence
                  + small projected-points and recency tiebreakers

    Each multiplier encodes something the raw projection cannot:

    - **value** - `vorp_z`, position-spread-dampened VORP (see `add_vorp_z`),
      so a steep replacement cliff (QB) doesn't dominate on raw scale alone.
      Falls back to raw `vorp` if the caller hasn't run `add_vorp_z()` yet.
    - **need** - which starting slots you still have open, escalating as
      `picks_remaining` runs out, plus what a bench spot at that position is
      worth once the lineup is full (see `need_weights` and `depth_needs`).
    - **timing** - the probability the player is gone before your next turn
      (see `availability_pressure`).
    - **confidence** - how much of the projection survives NB04's bootstrap
      5th-percentile floor (see `risk_multiplier`). Bounded tightly so it
      only reorders players the board already rates as close.

    Every component is returned as its own column alongside `utility`, so the
    ranking is inspectable rather than a single opaque number - the UI reads
    these directly to explain *why* a player is being recommended.
    """
    out = df.copy()

    w = need_weights(
        roster_state,
        picks_remaining=picks_remaining,
        depth=depth,
        bench_remaining=bench_remaining,
        teams=teams,
        missed_game_rate=missed_game_rate,
    )
    out["pos_weight"] = out["position"].map(lambda p: w.get(p, 1.0))

    # The need multiplier is two different claims added together - an unfilled
    # starting slot and a useful bench spot - and they mean opposite things to a
    # drafter. Returned separately so the UI can say "you still need a starting
    # TE" rather than "1.31x", and so a valuable backup RB is never described
    # with the same words as a wasted second kicker.
    starters = open_slots(roster_state)
    urgency = roster_urgency(roster_state, picks_remaining)
    bench = bench_weights(
        depth, bench_remaining, teams=teams, missed_game_rate=missed_game_rate
    )
    out["start_weight"] = out["position"].map(
        lambda p: NEED_WEIGHT * starters.get(p, 0.0) * urgency
    )
    out["bench_weight"] = out["position"].map(lambda p: bench.get(p, 0.0))
    out["adp_mult"] = out["league_pick_est"].apply(
        lambda x: pressure_fn(x, current_pick, next_pick)
    )
    out["availability"] = out["league_pick_est"].apply(lambda x: availability(x, next_pick))
    out["confidence"] = confidence(out)
    out["risk_mult"] = risk_multiplier(out, risk_aversion=risk_aversion)

    recency = out[recency_col] if recency_col in out.columns else 0.0
    value_col = vorp_col if vorp_col in out.columns else "vorp"
    out["base_value"] = out[value_col].clip(lower=0)
    out["utility"] = (
        out["base_value"] * out["pos_weight"] * out["adp_mult"] * out["risk_mult"]
        + PROJECTED_POINTS_WEIGHT * out["projected_points"]
        + RECENCY_WEIGHT * recency
    )
    return out.sort_values("utility", ascending=False)
