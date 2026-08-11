"""How this specific league's draft deviates from the national market.

Every player on the board carries a national ADP, but a league drafts to its own
habits. This module measures those habits and turns them into a **pick-timing**
adjustment: `league_pick_est` is where a player is expected to actually go *here*,
which feeds the availability and urgency multipliers in `src/scoring.py`.

**Timing only, never value.** This league taking Philadelphia players seventeen
picks earlier than the market does not make them better players — it means they
will be gone sooner, so you have to act earlier. Nothing in this module touches
VORP, projected points, or confidence. A player's worth is unchanged; only the
estimate of when they disappear moves.

What replaced the previous implementation
-----------------------------------------
The old version had two defects, both fixed here:

1. It fitted `k = median(actual_pick / market_adp)` and applied
   `k * adp + offset`. On this league's data `k` comes out to **exactly 1.0** —
   a knob that looks like it does something and does not. The effects are all
   additive pick deltas, so the model is now additive too, and `k` is gone.

2. It grouped by `drafts.position`, which is ESPN's *lineup slot*, not the
   player's position. 358 of 800 training rows are `"BE"`, plus more under
   `"RB/WR/TE"`. Those keys never match a pool position, so they were silently
   discarded by a `.get(pos, 0.0)` — meaning the RB offset was fitted only on
   RBs who happened to occupy a starting RB slot, systematically excluding the
   late and bench picks where reaching actually shows up. This is the same
   lineup-slot-versus-position bug the project already fixed in `players_stats`.
   The fit now reads `average_draft_position.position`.

What is measured, and what is applied
-------------------------------------
Fitted, persisted and shown in the app: position, NFL team, fantasy manager, and
per-player effects. **Applied to the board: position and NFL team only.** See
`apply_league_bias` for why the other two are deliberately left out.

Method notes, each of which changes the answer materially:

- **`adp <= 180`.** The ADP source ranks far more players than a 14-team draft
  can take (max ADP ranges from 253 in 2022 to 837 in 2025). Anyone ranked past
  the end of the board can only ever produce a large negative delta with no
  possible positive counterpart, which is a one-sided truncation artefact rather
  than a league habit. Including them drags every mean negative and inflates the
  spread by 50-80%.
- **Demeaned by season.** The league went from 12 teams to 14 in 2024
  (180 to 224 picks) and the ADP source mix changed, so raw pick numbers are not
  comparable across years.
- **Team and manager effects are residualised on position x year first**,
  otherwise "this manager reaches" is partly just "this manager drafts
  quarterbacks".
- **Managers are keyed on `teams.team_id`, never `team_name`.** Four franchises
  renamed themselves, and two of the names collide by a single space:
  "Gerald Pea's Football Team" (id 11, later "B Mac") and "GeraldPea's Football
  Team" (id 12, formerly "Jean Machine") are *different* franchises. Grouping by
  name merges two managers and splits two others.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
from sqlalchemy import text

from src.scoring import NO_ADP_SENTINEL, normalize_position

try:
    from notebooks.config import CURRENT_SEASON
except ImportError:  # pragma: no cover
    CURRENT_SEASON = 2025

# Beyond the end of a 14-team board, a "delta" is a truncation artefact rather
# than a preference. See the module docstring.
DEFAULT_ADP_MAX = 180.0

# Below two picks, a shift is not worth telling the user about - it is inside
# the noise of when any individual player actually goes.
MIN_REPORTABLE_SHIFT = 2.0

# `pro_team` values that are not NFL teams. Excluded from the fit explicitly
# rather than relying on "the key happens not to be in the dict" - that kind of
# silent passthrough is exactly how the lineup-slot bug survived. "FA" is what
# `load_candidates` fills for free agents (227 of 1026 candidates); "0" is junk
# in `players_stats` (23 rows in 2025).
JUNK_PRO_TEAMS = {"FA", "0", "", "None", "nan"}

# Measured over 2020-2025 (n=996 picks with a market price, adp<=180,
# year-demeaned), then shrunk - these are the applied numbers, not the raw
# means. Same contract as DEFAULT_MISSED_GAME_RATE in src/scoring.py: the live
# tool stays useful on a database where compute_league_bias.py has never run.
#
# QB -11.1 (t=-5.6) is the headline: this league takes quarterbacks roughly a
# full round earlier than the market, in every season on record. TE -6.2 and the
# K/DST fade are weaker but consistent. RB and WR sit near zero - the league and
# the market agree on the skill positions.
#
# Positions only. A per-NFL-team effect is a finding about *this* database and
# should not be frozen into source; it comes from the persisted table or not at
# all.
DEFAULT_LEAGUE_BIAS = {
    "position": {"QB": -11.1, "TE": -6.2, "DST": 7.1, "K": 6.1, "WR": 2.8, "RB": 1.1},
    "pro_team": {},
    "player": {},
    "meta": {"source": "default", "years": "2020-2025", "adp_cutoff": DEFAULT_ADP_MAX},
}

# Fallbacks for the empirical-Bayes shrinkage constants, used when the fit
# hasn't been persisted. Fitted on the current database: 7.9 and 32.7.
DEFAULT_POSITION_SHRINK_K = 8.0
DEFAULT_PROTEAM_SHRINK_K = 33.0

_FIT_SQL = """
SELECT
    d.player_id,
    d.year,
    d.overallPickNumber AS pick,
    d.team_id,
    a.avg               AS adp,
    a.position          AS position,
    s.pro_team          AS pro_team,
    p.player_name       AS player_name
FROM drafts d
JOIN average_draft_position a
     -- The CAST spelling must match `idx_adp_year_playerid` in src/indexes.py.
     ON CAST(a.player_id AS INTEGER) = d.player_id AND a.year = d.year
LEFT JOIN players_stats s ON s.player_id = d.player_id AND s.year = d.year
LEFT JOIN players p       ON p.player_id = d.player_id
WHERE a.avg IS NOT NULL AND a.avg > 0
"""

_BIAS_TABLES = ("league_bias_position", "league_bias_proteam",
                "league_bias_manager", "league_bias_player", "league_bias_meta")


# ---------------------------------------------------------------------------
# shrinkage
# ---------------------------------------------------------------------------

def _shrink(mean: float, n: int, k: float) -> float:
    """Pull a group mean toward zero in proportion to how little supports it.

    `mean * n / (n + k)`: a group with n >> k keeps almost all of its estimate,
    one with n << k keeps almost none. `k = 0` is a no-op, `k = inf` shrinks
    everything to zero.
    """
    if n is None or n <= 0 or mean is None or not np.isfinite(mean):
        return 0.0
    if math.isinf(k):
        return 0.0
    return float(mean) * n / (n + k)


def _empirical_bayes_k(means, ns, sigma2: float) -> float:
    """How hard to shrink, estimated from the data rather than picked.

    Splits the observed spread of group means into real between-group variance
    (`tau2`) and the sampling noise you would see even if every group were
    identical. `k = sigma2 / tau2`.

    Chosen over a significance gate because a hard `|t| > 2` cutoff would give a
    group with n=11 the same weight as one with n=31. This suppresses small and
    noisy groups smoothly, with no arbitrary threshold and no special cases.

    Returns `inf` when the observed spread is entirely explainable as noise,
    which shrinks every group to zero — the correct answer, and one that must
    not be a ZeroDivisionError.
    """
    means = np.asarray([m for m in means if m is not None and np.isfinite(m)], dtype=float)
    ns = np.asarray([n for n in ns if n], dtype=float)
    if len(means) < 2 or len(ns) != len(means) or sigma2 <= 0:
        return float("inf")
    observed = float(np.var(means, ddof=1))
    noise = sigma2 * float(np.mean(1.0 / ns))
    tau2 = max(observed - noise, 0.0)
    if tau2 <= 0:
        return float("inf")
    return sigma2 / tau2


def _group_stats(df: pd.DataFrame, key: str, value: str) -> pd.DataFrame:
    g = df.groupby(key)[value]
    out = g.agg(n="size", mean="mean", sd="std").reset_index()
    out["sd"] = out["sd"].fillna(0.0)
    # t is reported for the reader's benefit; nothing downstream gates on it.
    out["t"] = np.where(
        (out["n"] > 1) & (out["sd"] > 0),
        out["mean"] / (out["sd"] / np.sqrt(out["n"].clip(lower=1))),
        0.0,
    )
    return out


def _permutation_p(df: pd.DataFrame, key: str, value: str, n_perm: int, seed: int = 0) -> float | None:
    """Family-wise p for "any group differs", by shuffling the group labels.

    Compares the largest |t| actually observed against the largest |t| seen when
    the labels are randomised. This is the honest test when 32 NFL teams are
    examined at once: the biggest of 32 noise draws is routinely large.
    """
    if n_perm <= 0 or df[key].nunique() < 2:
        return None
    observed = _group_stats(df, key, value)["t"].abs().max()
    rng = np.random.default_rng(seed)
    labels = df[key].to_numpy()
    scratch = df[[key, value]].copy()
    hits = 0
    for _ in range(n_perm):
        scratch[key] = rng.permutation(labels)
        if _group_stats(scratch, key, value)["t"].abs().max() >= observed:
            hits += 1
    return (hits + 1) / (n_perm + 1)


# ---------------------------------------------------------------------------
# fitting
# ---------------------------------------------------------------------------

def _load_fit_frame(engine, years, adp_max: float) -> pd.DataFrame:
    df = pd.read_sql(text(_FIT_SQL), engine)
    if df.empty:
        return df
    df = df[df["year"].isin(list(years))]
    df = df[df["adp"] <= adp_max]
    df["position"] = df["position"].map(normalize_position)
    df = df[df["position"].notna()]
    df["delta"] = df["pick"] - df["adp"]
    # Season drift: league size and the ADP source mix both changed in 2024.
    df["delta_y"] = df["delta"] - df.groupby("year")["delta"].transform("mean")
    # Residual after position x year, so a team/manager effect isn't just that
    # they draft a lot of quarterbacks.
    df["resid"] = df["delta"] - df.groupby(["position", "year"])["delta"].transform("mean")
    return df


def _seasons_same_sign(df: pd.DataFrame, key: str, value: str) -> pd.Series:
    per_year = df.groupby([key, "year"])[value].mean().reset_index()
    return per_year.groupby(key)[value].apply(
        lambda s: int(max((s > 0).sum(), (s < 0).sum()))
    )


def fit_league_bias(engine, years=None, adp_max: float = DEFAULT_ADP_MAX,
                    permutations: int = 0) -> dict:
    """Measure this league's draft habits against the national market.

    Returns the applied shifts plus full diagnostics per family, ready to be
    persisted by `notebooks/compute_league_bias.py`.

    A note on honesty: shrinkage substantially mutes the headline finding. The
    raw Philadelphia effect is about -17 picks; after shrinking against 32 NFL
    teams of which most are null, it is applied as roughly -6. That is the right
    call under a mostly-null model, but it is a real reduction and is reported
    alongside the raw mean rather than quietly swapped for it.
    """
    years = tuple(years) if years else tuple(range(2020, CURRENT_SEASON + 1))
    df = _load_fit_frame(engine, years, adp_max)
    empty = {"position": {}, "pro_team": {}, "player": {}, "meta": {"source": "fit", "n_picks": 0},
             "tables": {k: pd.DataFrame() for k in ("position", "pro_team", "manager", "player")}}
    if df.empty:
        return empty

    seasons = ",".join(str(y) for y in sorted(df["year"].unique()))
    resid_sd = float(df["resid"].std())

    # --- position: measured on the season-demeaned delta ---
    pos = _group_stats(df, "position", "delta_y")
    k_pos = _empirical_bayes_k(pos["mean"], pos["n"], float(df["delta_y"].var()))
    pos["shrunk"] = [_shrink(m, n, k_pos) for m, n in zip(pos["mean"], pos["n"])]
    pos["seasons_same_sign"] = pos["position"].map(_seasons_same_sign(df, "position", "delta_y")).fillna(0).astype(int)
    pos["seasons"] = seasons
    pos["p_perm"] = _permutation_p(df, "position", "delta_y", permutations)

    # --- NFL team: on the position x year residual, junk keys dropped ---
    team_df = df[df["pro_team"].notna()]
    team_df = team_df[~team_df["pro_team"].astype(str).isin(JUNK_PRO_TEAMS)]
    if len(team_df):
        team = _group_stats(team_df, "pro_team", "resid")
        k_team = _empirical_bayes_k(team["mean"], team["n"], float(team_df["resid"].var()))
        team["shrunk"] = [_shrink(m, n, k_team) for m, n in zip(team["mean"], team["n"])]
        team["seasons_same_sign"] = team["pro_team"].map(
            _seasons_same_sign(team_df, "pro_team", "resid")).fillna(0).astype(int)
        team["seasons"] = seasons
        team["controlled"] = "position x year"
        team["p_perm"] = _permutation_p(team_df, "pro_team", "resid", permutations)
    else:
        team, k_team = pd.DataFrame(), float("inf")

    # --- manager: keyed on team_id, name carried for display only ---
    mgr_df = df[df["team_id"].notna()]
    if len(mgr_df):
        mgr = _group_stats(mgr_df, "team_id", "resid")
        k_mgr = _empirical_bayes_k(mgr["mean"], mgr["n"], float(mgr_df["resid"].var()))
        mgr["shrunk"] = [_shrink(m, n, k_mgr) for m, n in zip(mgr["mean"], mgr["n"])]
        mgr["seasons_n"] = mgr["team_id"].map(mgr_df.groupby("team_id")["year"].nunique()).fillna(0).astype(int)
        mgr["seasons"] = seasons
        mgr["controlled"] = "position x year"
        mgr["p_perm"] = _permutation_p(mgr_df, "team_id", "resid", permutations)
    else:
        mgr, k_mgr = pd.DataFrame(), float("inf")

    # --- per-player: strict filter, reported but not applied ---
    ply = (df.groupby(["player_id", "player_name"])["delta_y"]
             .agg(n="size", mean="mean", sd="std").reset_index())
    ply["sd"] = ply["sd"].fillna(0.0)
    ply["seasons"] = seasons
    same = _seasons_same_sign(df, "player_id", "delta_y")
    ply["seasons_same_sign"] = ply["player_id"].map(same).fillna(0).astype(int)
    # n>=3, every season the same direction, and a spread smaller than the
    # effect. Deliberately strict, and still selecting on the same data that
    # produced it - see apply_league_bias for why none of this is applied.
    ply["passes_strict_filter"] = (
        (ply["n"] >= 3)
        & (ply["seasons_same_sign"] == ply["n"])
        & (ply["sd"] < ply["mean"].abs())
    ).astype(int)

    # Per-season means, long format. Persisted because consistency is the whole
    # argument: a -11 quarterback effect in every one of six seasons is a habit,
    # while the same number driven by one wild year is an accident, and an
    # aggregate mean cannot tell those apart.
    by_season = (df.groupby(["position", "year"])["delta_y"]
                   .agg(n="size", mean="mean").reset_index())
    team_by_season = (team_df.groupby(["pro_team", "year"])["resid"]
                        .agg(n="size", mean="mean").reset_index()) if len(team_df) else pd.DataFrame()

    return {
        "position": {r.position: float(r.shrunk) for r in pos.itertuples()},
        "pro_team": {r.pro_team: float(r.shrunk) for r in team.itertuples()} if len(team) else {},
        "player": {int(r.player_id): float(r.mean) for r in ply.itertuples() if r.passes_strict_filter},
        "meta": {
            "source": "fit", "years": seasons, "adp_cutoff": float(adp_max),
            "n_picks": int(len(df)), "resid_sd": resid_sd,
            "k_position": float(k_pos), "k_proteam": float(k_team), "k_manager": float(k_mgr),
        },
        "tables": {"position": pos, "pro_team": team, "manager": mgr, "player": ply,
                   "position_season": by_season, "proteam_season": team_by_season},
    }


# ---------------------------------------------------------------------------
# loading
# ---------------------------------------------------------------------------

def _has_table(engine, name: str) -> bool:
    q = text("SELECT name FROM sqlite_master WHERE type='table' AND name=:n")
    with engine.connect() as conn:
        return conn.execute(q, {"n": name}).first() is not None


def load_league_bias(engine) -> dict:
    """Read the persisted fit, or fall back to the measured constants.

    Called at API import instead of `fit_league_bias`, because fitting needs
    `teams`, `players` and `players_stats.pro_team` — none of which a partially
    built database (or the test fixture) has — and because a permutation test
    does not belong on the boot path.
    """
    if not _has_table(engine, "league_bias_position"):
        return dict(DEFAULT_LEAGUE_BIAS)

    def read(name):
        if not _has_table(engine, name):
            return pd.DataFrame()
        return pd.read_sql(text(f"SELECT * FROM {name}"), engine)

    pos = read("league_bias_position")
    team = read("league_bias_proteam")
    ply = read("league_bias_player")
    meta_df = read("league_bias_meta")

    out = {
        "position": {r.position: float(r.shrunk) for r in pos.itertuples()} if len(pos) else {},
        "pro_team": {r.pro_team: float(r.shrunk) for r in team.itertuples()} if len(team) else {},
        "player": {},
        "meta": {"source": "persisted"},
    }
    if len(ply) and "passes_strict_filter" in ply.columns:
        out["player"] = {int(r.player_id): float(r.mean)
                         for r in ply.itertuples() if r.passes_strict_filter}
    if len(meta_df):
        out["meta"].update({k: v for k, v in meta_df.iloc[0].to_dict().items()
                            if not pd.isna(v)})
    return out


# ---------------------------------------------------------------------------
# applying
# ---------------------------------------------------------------------------

def _reason(pos_shift: float, team_shift: float, position, pro_team) -> str | None:
    """The one sentence shown to the user, composed here rather than in the UI.

    Kept server-side so the notebooks, the API and the interface all describe an
    effect the same way — the same reason `normalize_position` lives in one place.
    """
    def phrase(subject: str, shift: float) -> str:
        n = round(abs(shift))
        return (f"{subject} go about {n} pick{'' if n == 1 else 's'} "
                f"{'earlier' if shift < 0 else 'later'} here")

    # Only components that carry their own weight get named. A part worth a
    # pick and a half rounds to "about 1 pick", which reads as precision the
    # estimate doesn't have and clutters the sentence - so it's left out even
    # when the total clears the reporting threshold.
    parts = []
    if abs(team_shift) >= 1.5 and pro_team:
        parts.append(phrase(f"{pro_team} players", team_shift))
    if abs(pos_shift) >= 1.5 and position:
        parts.append(phrase(f"{position}s", pos_shift))
    if not parts:
        return None
    return " and ".join(parts) + " than national ADP"


def apply_league_bias(df: pd.DataFrame, bias: dict, include_player: bool = False) -> pd.DataFrame:
    """Add `league_pick_est` and the bias breakdown to a candidate pool.

        league_pick_est = clip(adp + position_shift + pro_team_shift, lower=1)

    **Position and NFL team only.** Two measured effects are deliberately left
    out of the applied shift:

    - *Manager* effects are real (family-wise p = 0.002) but attach to a
      **drafter**, not a player. The tool has no idea which of the other
      thirteen managers is on the clock at any given pick, so there is nothing
      to attach the shift to, and averaging over all of them is zero by
      construction. Fitted, persisted and shown — not applied.
    - *Per-player* effects are mostly noise: n is 2-4 against a per-pick spread
      of about 19 picks, and the strict filter selects on the same data that
      produced it. `include_player=True` enables them for experimentation; it is
      off by default on purpose.
    """
    out = df.copy()
    pos_map = bias.get("position", {}) or {}
    team_map = bias.get("pro_team", {}) or {}
    player_map = (bias.get("player", {}) or {}) if include_player else {}

    pos_shift = out["position"].map(lambda p: float(pos_map.get(p, 0.0))).fillna(0.0)

    if "pro_team" in out.columns:
        team_shift = out["pro_team"].map(
            lambda t: 0.0 if (t is None or str(t) in JUNK_PRO_TEAMS) else float(team_map.get(t, 0.0))
        ).fillna(0.0)
    else:
        team_shift = pd.Series(0.0, index=out.index)

    shift = pos_shift + team_shift
    if player_map and "player_id" in out.columns:
        shift = shift + out["player_id"].map(lambda i: float(player_map.get(i, 0.0))).fillna(0.0)

    adp = pd.to_numeric(out["adp"], errors="coerce").fillna(NO_ADP_SENTINEL)
    # A player with no market data carries a sentinel, not a pick number.
    # Shifting it still happens to clear the threshold today, but only by
    # coincidence - make it explicit so the two constants aren't silently
    # coupled.
    no_market = adp >= NO_ADP_SENTINEL
    shift = shift.where(~no_market, 0.0)
    pos_shift = pos_shift.where(~no_market, 0.0)
    team_shift = team_shift.where(~no_market, 0.0)

    # Nobody goes before pick 1: a quarterback with ADP 3 and a -9.9 shift must
    # not produce a negative pick estimate.
    out["league_pick_est"] = (adp + shift).clip(lower=1.0)
    out["bias_shift"] = shift.astype(float)
    out["bias_pos_shift"] = pos_shift.astype(float)
    out["bias_team_shift"] = team_shift.astype(float)

    positions = out["position"] if "position" in out.columns else pd.Series(None, index=out.index)
    teams = out["pro_team"] if "pro_team" in out.columns else pd.Series(None, index=out.index)
    out["bias_reason"] = [
        _reason(p, t, po, te) if abs(s) >= MIN_REPORTABLE_SHIFT else None
        for s, p, t, po, te in zip(shift, pos_shift, team_shift, positions, teams)
    ]
    return out
