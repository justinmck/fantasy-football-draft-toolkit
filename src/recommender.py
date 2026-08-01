import numpy as np
import pandas as pd
from sqlalchemy import inspect, text

from src.biases import apply_league_bias
from src.scoring import (
    DEFAULT_ROOKIE_RELIABILITY_FACTOR,
    RISK_AVERSION,
    add_vorp,
    add_vorp_z,
    compute_baselines,
    normalize_position,
    score,
)


def _has_table(engine, name: str) -> bool:
    return inspect(engine).has_table(name)


def resolve_adp_year(engine, year: int) -> int | None:
    """The most recent season with ADP data at or before `year`.

    ADP for an upcoming season isn't published until a few weeks before the
    draft, so joining strictly on `year` returns nothing all offseason - every
    player falls back to the "no market data" sentinel, and pick timing goes
    completely inert (every player reads as 100% likely to still be there).

    Falling back to the latest season on record keeps the timing signal usable
    year-round. It's the same fallback NB02 already applies to projections, and
    last year's ADP is a far better estimate of where a player goes than no
    estimate at all. Re-run NB01/NB02 once the real ADP is out.
    """
    if not _has_table(engine, "average_draft_position"):
        return None
    row = pd.read_sql(
        text("SELECT MAX(year) AS y FROM average_draft_position WHERE year <= :year"),
        engine,
        params={"year": year},
    )
    y = row["y"].iloc[0] if len(row) else None
    return int(y) if y is not None and not pd.isna(y) else None


def _rookie_factor(engine) -> float:
    """How much a rookie's projection is worth relative to a veteran's at the
    same position, as measured by NB05 (R² 0.282 vs 0.463 over 2021-2025).

    Falls back to the constant in src/scoring.py when NB05 hasn't been run.
    """
    if not _has_table(engine, "rookie_reliability"):
        return DEFAULT_ROOKIE_RELIABILITY_FACTOR
    row = pd.read_sql(
        text("SELECT reliability_factor FROM rookie_reliability WHERE \"group\" = 'rookie'"),
        engine,
    )
    if not len(row) or pd.isna(row["reliability_factor"].iloc[0]):
        return DEFAULT_ROOKIE_RELIABILITY_FACTOR
    return float(row["reliability_factor"].iloc[0])


def load_candidates(engine, year, drafted_ids):
    """Every undrafted player projected for `year`, with the market's view of
    them and - when NB04 has been run - the model's prediction interval.

    The `model_projections` join is LEFT and guarded by an existence check so
    the live API still works on a database where NB04 hasn't run yet (or on the
    test fixture); missing intervals just mean `score()` falls back to
    treating every player as equally certain.
    """
    extra_cols, extra_joins = "", ""
    if _has_table(engine, "model_projections"):
        extra_cols += """,
               mp.predicted_vorp, mp.ci_low, mp.ci_high"""
        extra_joins += """
    LEFT JOIN model_projections mp
        ON mp.player_id = pr.player_id AND mp.year = pr.year"""
    if _has_table(engine, "position_reliability"):
        extra_cols += """,
               pxr.reliability, pxr.rmse AS position_rmse"""
        extra_joins += """
    LEFT JOIN position_reliability pxr
        ON pxr.position = pr.position"""

    # average_draft_position.player_id is declared CHAR(7), so it comes back as
    # text while next_season_projections.player_id is an integer - without the
    # CAST the join silently matches nothing.
    adp_year = resolve_adp_year(engine, year)
    q = f"""
    SELECT pr.player_id, pr.player_name, pr.position, pr.pro_team,
           pr.projected_points, ps.avg_points AS avg_last_year,
           ps.games_played AS games_last_year,
           ps.actual_receivingTargets AS targets_last_year,
           ps.actual_rushingAttempts  AS rush_att_last_year,
           ps.actual_passingAttempts  AS pass_att_last_year,
           ps.points AS points_last_year,
           ps.player_id AS prior_season_player_id,
           COALESCE(adp.avg, 999.0) AS adp{extra_cols}
    FROM next_season_projections pr
    LEFT JOIN average_draft_position adp
        ON CAST(adp.player_id AS INTEGER) = pr.player_id AND adp.year = :adp_year
    LEFT JOIN players_stats ps
        ON ps.player_id = pr.player_id AND ps.year = pr.year - 1{extra_joins}
    WHERE pr.year = :year
    """
    df = pd.read_sql(text(q), engine, params={"year": year, "adp_year": adp_year})
    df["position"] = df["position"].map(normalize_position)

    # A player with no prior-season row has no production history for the
    # projection to build on. Same definition NB04's `is_rookie` feature and
    # NB05's rookie analysis use, so all three agree on who counts.
    df["is_rookie"] = df["prior_season_player_id"].isna().astype(int)
    df = df.drop(columns="prior_season_player_id")
    df["rookie_factor"] = _rookie_factor(engine)

    df["avg_last_year"] = df["avg_last_year"].fillna(0.0)
    df["pro_team"] = df["pro_team"].fillna("FA")
    # Which season's market the timing signal is actually based on, so the API
    # (and the UI) can say so rather than implying it's this year's ADP.
    df.attrs["adp_year"] = adp_year
    if drafted_ids:
        df = df[~df.player_id.isin(drafted_ids)]
    return df


# Columns the API returns. Three groups, all deliberate:
#
#   identity + projection - what the player is and what they're expected to do
#   last season's actuals - the evidence behind the projection, so the UI's
#                           player detail panel can show it without a second call
#   scoring components    - the ranking is a product of four terms, and
#                           returning only the product would leave the UI unable
#                           to say *why* a player is recommended
RESULT_COLUMNS = [
    "player_id", "player_name", "position", "pro_team", "is_rookie",
    "projected_points", "vorp", "vorp_z", "adp", "league_pick_est",
    "avg_last_year", "games_last_year", "points_last_year",
    "targets_last_year", "rush_att_last_year", "pass_att_last_year",
    "predicted_vorp", "ci_low", "ci_high", "reliability", "position_rmse",
    "pos_weight", "adp_mult", "risk_mult", "availability", "confidence",
    "base_value", "utility",
]


def recommend(engine, year, session, current_pick, next_pick, bias, topn=10,
              risk_aversion=RISK_AVERSION):
    pool = load_candidates(engine, year, drafted_ids=session.drafted_ids)
    baselines = compute_baselines(pool, teams=session.teams)
    pool = add_vorp(pool, baselines)
    # vorp_z dampens positions with a steep replacement-level cliff (e.g. QB)
    # so ranking isn't skewed by raw-points scale differences across
    # positions - see src/scoring.py's add_vorp_z docstring. Raw `vorp` is
    # still returned below, unchanged, so old vs. new can be compared.
    pool = add_vorp_z(pool, teams=session.teams)
    pool = apply_league_bias(pool, bias) if bias else pool.assign(league_pick_est=pool.adp)
    ranked = score(
        pool,
        session.roster_state,
        current_pick,
        next_pick,
        # Lets need weighting escalate as the drafter runs out of picks; None
        # when the session doesn't know the draft length, which reduces need
        # weighting to its previous, non-urgency behaviour.
        picks_remaining=session.picks_remaining(),
        risk_aversion=risk_aversion,
    )
    cols = [c for c in RESULT_COLUMNS if c in ranked.columns]
    # NaN isn't valid JSON and 500s the endpoint - rookies legitimately have no
    # prior stats and players outside the model's pool have no interval.
    out = ranked[cols].head(topn).replace({np.nan: None})
    out.attrs["adp_year"] = pool.attrs.get("adp_year")
    return out
