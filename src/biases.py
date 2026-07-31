import pandas as pd
from sqlalchemy import bindparam, text

from src.scoring import normalize_position

try:
    from notebooks.config import CURRENT_SEASON
except ImportError:  # pragma: no cover
    CURRENT_SEASON = 2025


def fit_league_bias(engine, years=None):
    """Learn how this specific league's draft tends to deviate from
    national ADP (e.g. a league that reaches for QBs earlier than average).

    CAVEAT: this is fit on a single 14-team league across a handful of
    seasons, so it's a small, noisy sample - treat it as a mild, illustrative
    correction on top of ADP, not a statistically robust model. Every prior
    season on record is used by default (rather than a fixed two-year
    window) simply to use what little data exists as fully as possible.
    """
    years = years or tuple(range(CURRENT_SEASON - 4, CURRENT_SEASON + 1))
    q = text("""
    SELECT d.overallPickNumber AS actual_pick, d.position, adp.avg AS market_adp
    FROM drafts d
    LEFT JOIN average_draft_position adp
        ON adp.player_id = d.player_id AND adp.year = d.year
    WHERE d.year IN :years
      AND adp.avg IS NOT NULL
    """).bindparams(bindparam("years", expanding=True))
    df = pd.read_sql(q, engine, params={"years": list(years)})
    df["position"] = df["position"].map(normalize_position)
    df = df[(df.market_adp > 0)]
    if df.empty:
        return {"k": 1.0, "b_pos": {}}
    k = (df.actual_pick / df.market_adp).median()
    df["resid"] = df.actual_pick - k * df.market_adp
    b_pos = df.groupby("position")["resid"].median().to_dict()
    return {"k": float(k), "b_pos": b_pos}

def apply_league_bias(df, bias):
    k = bias.get("k", 1.0); b = bias.get("b_pos", {})
    out = df.copy()
    out["league_pick_est"] = k*out["adp"] + out["position"].map(lambda p: b.get(p, 0.0))
    out["league_pick_est"] = out["league_pick_est"].fillna(999.0)
    return out
