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


def _missed_game_rates(engine) -> dict | None:
    """Per-position rate at which startable players actually missed games.

    Drives how much a bench spot at each position is worth (see
    `src/scoring.py`'s `depth_needs`). Written by
    `notebooks/compute_availability.py`; returning None falls back to the
    measured defaults in scoring.py, so the live tool still runs on a database
    where that script hasn't been run.
    """
    if not _has_table(engine, "position_availability"):
        return None
    df = pd.read_sql(
        text("SELECT position, missed_game_rate FROM position_availability"), engine
    )
    df = df.dropna(subset=["position", "missed_game_rate"])
    if df.empty:
        return None
    return {normalize_position(p): float(r) for p, r in zip(df["position"], df["missed_game_rate"])}


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


def resolve_stats_league(engine, year: int, league_id: str | None) -> str | None:
    """Which league's player-seasons the board should read for `year`.

    `players_stats` is per league, because scoring settings differ - the same
    player-season is worth different points in two leagues. That makes the join
    below one-to-many unless a league is named: with two leagues stored for
    2025, every player who appears in both produced *two* rows in the pool and
    the board listed them twice.

    Prefers the league being drafted. When it has no stored seasons - a
    brand-new league, the common case - it borrows the fullest league's rows
    rather than treating every player as a rookie with no history, which is
    what an empty join would silently do. The borrowed figures are last
    season's production; only the points columns are scoring-dependent.

    Returns None when the column doesn't exist (a database from before the
    migration, or a test fixture), which leaves the query unfiltered.
    """
    if not _has_table(engine, "players_stats"):
        return None
    if "league_id" not in {c["name"] for c in inspect(engine).get_columns("players_stats")}:
        return None
    rows = pd.read_sql(
        text("""SELECT league_id, COUNT(*) AS n FROM players_stats
                WHERE year = :year AND league_id IS NOT NULL GROUP BY league_id"""),
        engine, params={"year": year},
    )
    if rows.empty:
        return None
    stored = {str(v) for v in rows["league_id"]}
    if league_id and str(league_id) in stored:
        return str(league_id)
    return str(rows.sort_values("n", ascending=False)["league_id"].iloc[0])


def load_candidates(engine, year, drafted_ids, league_id=None):
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
    #
    # The CAST spelling must match `idx_adp_year_playerid` in src/indexes.py
    # exactly: SQLite only uses an expression index when the query spells the
    # expression identically, and this is the hottest query in the app.
    adp_year = resolve_adp_year(engine, year)
    # Without this the players_stats join fans out across leagues - see
    # resolve_stats_league.
    stats_league = resolve_stats_league(engine, year - 1, league_id)
    stats_filter = " AND ps.league_id = :stats_league" if stats_league else ""
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
        ON ps.player_id = pr.player_id AND ps.year = pr.year - 1{stats_filter}{extra_joins}
    WHERE pr.year = :year
    """
    params = {"year": year, "adp_year": adp_year}
    if stats_league:
        params["stats_league"] = stats_league
    df = pd.read_sql(text(q), engine, params=params)
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
    # How this league's own habits move the expected pick, split into its parts.
    # Returned separately for the same reason the scoring multipliers are: the
    # total alone can't tell the UI whether it's "your league loves Eagles" or
    # "your league loves quarterbacks". `bias_reason` is composed server-side so
    # notebooks and interface describe an effect identically.
    "bias_shift", "bias_pos_shift", "bias_team_shift", "bias_reason",
    # This player's own draft record in this league, where there is enough of
    # one to report. Evidence shown beside the estimate, never folded into it.
    "bias_player_shift", "bias_player_n",
    "avg_last_year", "games_last_year", "points_last_year",
    "targets_last_year", "rush_att_last_year", "pass_att_last_year",
    "predicted_vorp", "ci_low", "ci_high", "reliability", "position_rmse",
    "pos_weight", "start_weight", "bench_weight",
    "adp_mult", "risk_mult", "availability", "confidence",
    "base_value", "utility",
]


def recommend(engine, year, session, current_pick, next_pick, bias, topn=10,
              risk_aversion=RISK_AVERSION, league_id=None):
    pool = load_candidates(engine, year, drafted_ids=session.drafted_ids,
                           league_id=league_id)
    baselines = compute_baselines(pool, teams=session.teams)
    pool = add_vorp(pool, baselines)
    # vorp_z dampens positions with a steep replacement-level cliff (e.g. QB)
    # so ranking isn't skewed by raw-points scale differences across
    # positions - see src/scoring.py's add_vorp_z docstring. Raw `vorp` is
    # still returned below, unchanged, so old vs. new can be compared.
    pool = add_vorp_z(pool, teams=session.teams)
    # Always applied, even with no bias to apply: an empty fit yields
    # `league_pick_est == adp`, zero shifts and a null reason, which keeps the
    # response shape identical whether or not this league has a measured
    # history. A league without one shouldn't return differently-shaped rows.
    pool = apply_league_bias(pool, bias or {})
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
        # Bench context: which positions the drafter already has spare bodies
        # at, and whether there's any room left to add more. Without these the
        # need term collapses to 1.0 for every position the moment the starting
        # lineup is full - i.e. for the whole back half of the draft.
        depth=session.depth,
        bench_remaining=session.bench_remaining(),
        teams=session.teams,
        missed_game_rate=_missed_game_rates(engine),
    )
    cols = [c for c in RESULT_COLUMNS if c in ranked.columns]
    # NaN isn't valid JSON and 500s the endpoint - rookies legitimately have no
    # prior stats and players outside the model's pool have no interval.
    out = ranked[cols].head(topn).replace({np.nan: None})
    out.attrs["adp_year"] = pool.attrs.get("adp_year")
    return out
