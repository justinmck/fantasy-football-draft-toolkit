import pandas as pd
from sqlalchemy import text
def load_candidates(engine, year, drafted_ids):
    q = """
    SELECT pr.player_id, p.player_name, p.position, p.pro_team,
           pr.projected_points, COALESCE(adp.avg, 999.0) AS adp
    FROM next_season_projections pr
    JOIN players p ON p.player_id = pr.player_id
    LEFT JOIN average_draft_position adp ON adp.player_id = pr.player_id
    WHERE pr.year = :year                         -- CHANGED
    """
    df = pd.read_sql(text(q), engine, params={"year": year})
    if drafted_ids:
        df = df[~df.player_id.isin(drafted_ids)]
    return df

def compute_baselines(df, teams=14):
    needs = {'QB':teams*1, 'RB':teams*2, 'WR':teams*2, 'TE':teams*1}
    base = {}
    for pos, n in needs.items():
        pool = df[df.position==pos].sort_values("projected_points", ascending=False).head(n)
        base[pos] = pool.projected_points.min() if len(pool) else 0.0
    return base

def add_vorp(df, baselines):
    out = df.copy()
    out["baseline"] = out.position.map(lambda p: baselines.get(p, 0.0))
    out["projected_vorp"] = out.projected_points - out.baseline
    return out

def need_weights(roster_state):
    return {pos: 1.0 + 0.5*max(v["need"]-v["have"], 0) for pos, v in roster_state.items()}

def adp_pressure(league_pick_est, current_pick, next_pick):
    if league_pick_est <= current_pick: return 1.12
    if league_pick_est <  next_pick:    return 1.22
    if league_pick_est <  next_pick+12: return 1.05
    return 0.95

def score(df, roster_state, current_pick, next_pick):
    w = need_weights(roster_state)
    out = df.copy()
    out["pos_weight"] = out.position.map(lambda p: w.get(p, 1.0))
    out["adp_mult"] = out.league_pick_est.apply(lambda x: adp_pressure(x, current_pick, next_pick))
    out["utility"] = (out.projected_vorp.clip(lower=0) * out.pos_weight * out.adp_mult
                      + 0.02 * out.projected_points)
    return out.sort_values("utility", ascending=False)

def recommend(engine, year, session, current_pick, next_pick, bias, topn=10):
    pool = load_candidates(engine, year, drafted_ids=session.drafted_ids)
    baselines = compute_baselines(pool, teams=session.teams)
    pool = add_vorp(pool, baselines)
    from src.biases import apply_league_bias
    pool = apply_league_bias(pool, bias) if bias else pool.assign(league_pick_est=pool.adp)
    ranked = score(pool, session.roster_state, current_pick, next_pick)
    return ranked[["player_id","player_name","position","pro_team",
                   "projected_points","projected_vorp","adp","league_pick_est","utility"]].head(topn)
