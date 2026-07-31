import pandas as pd
from sqlalchemy import text

from src.biases import apply_league_bias
from src.scoring import add_vorp, add_vorp_z, compute_baselines, normalize_position, score


def load_candidates(engine, year, drafted_ids):
    q = """
    SELECT pr.player_id, pr.player_name, pr.position, pr.pro_team,
           pr.projected_points, ps.avg_points AS avg_last_year,
           COALESCE(adp.avg, 999.0) AS adp
    FROM next_season_projections pr
    LEFT JOIN average_draft_position adp
        ON adp.player_id = pr.player_id AND adp.year = pr.year
    LEFT JOIN players_stats ps
        ON ps.player_id = pr.player_id AND ps.year = pr.year - 1
    WHERE pr.year = :year
    """
    df = pd.read_sql(text(q), engine, params={"year": year})
    df["position"] = df["position"].map(normalize_position)
    df["avg_last_year"] = df["avg_last_year"].fillna(0.0)
    df["pro_team"] = df["pro_team"].fillna("FA")
    if drafted_ids:
        df = df[~df.player_id.isin(drafted_ids)]
    return df


def recommend(engine, year, session, current_pick, next_pick, bias, topn=10):
    pool = load_candidates(engine, year, drafted_ids=session.drafted_ids)
    baselines = compute_baselines(pool, teams=session.teams)
    pool = add_vorp(pool, baselines)
    # vorp_z dampens positions with a steep replacement-level cliff (e.g. QB)
    # so ranking isn't skewed by raw-points scale differences across
    # positions - see src/scoring.py's add_vorp_z docstring. Raw `vorp` is
    # still returned below, unchanged, so old vs. new can be compared.
    pool = add_vorp_z(pool, teams=session.teams)
    pool = apply_league_bias(pool, bias) if bias else pool.assign(league_pick_est=pool.adp)
    ranked = score(pool, session.roster_state, current_pick, next_pick)
    return ranked[["player_id", "player_name", "position", "pro_team",
                    "projected_points", "vorp", "vorp_z", "adp", "league_pick_est", "utility"]].head(topn)
