import pandas as pd
from sqlalchemy import text

def fit_league_bias(engine, years=(2023, 2024)):
    q = """
    SELECT d.overallPickNumber AS actual_pick, p.position, adp.avg AS market_adp
    FROM drafts d
    JOIN players p ON p.player_id = d.player_id
    LEFT JOIN average_draft_position adp ON adp.player_id = d.player_id
    WHERE d.year IN :years                       -- CHANGED
      AND adp.avg IS NOT NULL
    """
    df = pd.read_sql(text(q), engine, params={"years": tuple(years)})
    df = df[(df.market_adp > 0)]
    k = (df.actual_pick / df.market_adp).median()
    df["resid"] = df.actual_pick - k*df.market_adp
    b_pos = df.groupby("position")["resid"].median().to_dict()
    return {"k": float(k), "b_pos": b_pos}

def apply_league_bias(df, bias):
    k = bias.get("k", 1.0); b = bias.get("b_pos", {})
    out = df.copy()
    out["league_pick_est"] = k*out["adp"] + out["position"].map(lambda p: b.get(p, 0.0))
    out["league_pick_est"] = out["league_pick_est"].fillna(999.0)
    return out
