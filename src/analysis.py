"""The whole analytical case behind the draft board, served to the Analysis tab.

Five notebooks produce this project's findings: NB01 pulls league history out of
ESPN, NB02 cleans it, NB03 measures who actually drafted well, NB04 fits and
validates a next-season projection, and NB05 grades how much that projection was
worth. This module reassembles all of it into one payload so the app can present
the reasoning rather than asking a reader to open five notebooks.

**Everything is computed at request time or read from a persisted table — none
of it is hardcoded.** The notebooks rewrite these tables whenever the data is
refreshed, and a number typed into the frontend would silently drift away from
what the board is actually doing. Where a figure is expensive to recompute
(NB04's model comparison and ablation, NB05's reliability tables), the notebook
persists it and this module reads it behind an existence check, so a database
where a given notebook has never run degrades to "not available" rather than
erroring.

VORP everywhere comes from `src.scoring` — the same replacement-baseline
definition the live recommender uses — so "who drafted best" is measured on
exactly the scale the board ranks players by.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
# Imported at module level, not lazily inside `_pearson`. The lazy import cost
# 1.2s on the first `/analysis` request - a user-visible stall - and bought
# nothing, since scipy is a hard dependency that scikit-learn pulls in anyway.
# Paying it at boot puts it where nobody is waiting.
#
# Only `pearsonr` is used, and its `r` is a one-liner in numpy - but the
# p-value needs a Student-t survival function at df = n-2, and
# `draft_performance` correlates over 14 teams, exactly where a normal
# approximation is wrong. Hand-rolling an incomplete beta is not worth it.
from scipy import stats
from sqlalchemy import text

from notebooks.config import CURRENT_SEASON, POSITIONS, ROSTER_NEEDS, TEAMS
from src.migrations import known_leagues, seasons_for_league
from src.scoring import (
    NO_ADP_SENTINEL,
    add_vorp,
    compute_baselines,
    depth_needs,
    normalize_position,
    starters_needed,
)

# 2023's projections are a known extraction gap: `projected_points` was recorded
# as 0.0 for 392 of 480 players (Patrick Mahomes shows points=267, projection=0).
# The rows that survive a zero-filter aren't a random sample of the season, so
# grading that year would report an artefact of which rows happened to survive.
BROKEN_PROJECTION_YEARS = (2023,)

_DRAFT_SEASON_SQL = """
SELECT
    d.player_id,
    p.player_name,
    s.points,
    s.projected_points,
    s.games_played,
    d.team_id,
    t.team_name,
    t.final_standing,
    a.position,
    a.avg               AS adp,
    d.overallPickNumber AS pick,
    d.roundId           AS round,
    (d.overallPickNumber - a.avg) AS draft_delta
FROM drafts d
JOIN players p       ON d.player_id = p.player_id
JOIN players_stats s ON d.player_id = s.player_id AND d.year = s.year
JOIN teams t         ON d.team_id   = t.team_id   AND d.year = t.year
JOIN average_draft_position a
     -- The CAST spelling must match `idx_adp_year_playerid` in src/indexes.py
     -- exactly, or SQLite silently stops using the expression index.
     ON CAST(a.player_id AS INTEGER) = d.player_id AND a.year = d.year
WHERE d.year = :year
  -- Every join here is league-scoped: a draft pick, the team that made it and
  -- the fantasy points it earned all belong to one league. Without this the
  -- three leagues' rows would be silently pooled, and since scoring settings
  -- differ the points wouldn't even be on a common scale.
  AND d.league_id = :league_id
  AND s.league_id = :league_id
  AND t.league_id = :league_id
"""

_PROJECTION_ACTUALS_SQL = """
SELECT player_id, position, projected_points, points AS actual_points
FROM players_stats
WHERE year = :year
  AND league_id = :league_id
  AND projected_points IS NOT NULL AND projected_points != 0
  AND points IS NOT NULL
"""

_REQUIRED_DRAFT_TABLES = ("drafts", "players", "players_stats", "teams", "average_draft_position")
# The accuracy/benchmark sections read more of `players_stats` than the
# recommender does. Checked explicitly rather than assumed, because a partially
# built database (or the test fixture, which carries only the recommender's
# columns) is a legitimate state that should render "not available" rather than
# raising OperationalError out of a GET.
_PROJECTION_COLUMNS = ("position", "projected_points", "points")


# ---------------------------------------------------------------------------
# request scope
# ---------------------------------------------------------------------------

class _Ctx:
    """One request's worth of repeated reads.

    The section functions below are independently useful — the notebooks import
    several of them directly — so each one loads whatever it needs. Assembled
    into a single payload, that meant `load_draft_season` running four times and
    `_projection_actuals` twice, returning byte-identical results, plus 35
    separate connections just to ask whether a table exists.

    **Request-scoped, deliberately not process-scoped.** The notebooks drop and
    recreate these tables, so a cache that outlived a request would serve
    numbers from a database that no longer exists. Everything held here is
    derived and never authoritative: discarding it costs a re-query, never
    correctness.

    Passed in wherever an `engine` would be, so no section signature changes and
    every function still works when handed a bare engine.
    """

    def __init__(self, engine, league_id=None, roster_need=None):
        self.engine = engine
        # Which league everything on this page describes. Held on the context
        # so it can't be forgotten by one section and applied by another.
        self.league_id = str(league_id) if league_id else None
        # That league's own starting lineup. Replacement level is defined by
        # who starts, so a league with a different lineup gets different
        # baselines and therefore a different VORP for every player.
        self.roster_need = dict(roster_need) if roster_need else dict(ROSTER_NEEDS)
        self._tables = None
        self._columns = {}
        self._draft = {}
        self._actuals = {}

    def connect(self):
        return self.engine.connect()

    def tables(self) -> set:
        if self._tables is None:
            with self.engine.connect() as conn:
                rows = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
                self._tables = {r[0] for r in rows}
        return self._tables

    def columns(self, table: str) -> set:
        if table not in self._columns:
            with self.engine.connect() as conn:
                rows = conn.execute(text(f"PRAGMA table_info({table})"))
                self._columns[table] = {r[1] for r in rows}
        return self._columns[table]


def _ctx(engine, league_id=None, roster_need=None) -> _Ctx:
    """Accept either a context or a bare engine.

    Given an engine this builds a throwaway context, so a standalone call is no
    slower than it was — it just doesn't share anything with its neighbors.
    """
    return engine if isinstance(engine, _Ctx) else _Ctx(engine, league_id, roster_need)


def _engine_of(engine):
    return engine.engine if isinstance(engine, _Ctx) else engine


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _has_table(engine, name: str) -> bool:
    return name in _ctx(engine).tables()


def _has_columns(engine, table: str, columns) -> bool:
    ctx = _ctx(engine)
    if table not in ctx.tables():
        return False
    return set(columns) <= ctx.columns(table)


def _read_table(engine, name: str, order_by: str | None = None) -> list[dict]:
    """A persisted notebook output, or [] when that notebook hasn't been run."""
    if not _has_table(engine, name):
        return []
    sql = f"SELECT * FROM {name}" + (f" ORDER BY {order_by}" if order_by else "")
    return _records(pd.read_sql(text(sql), _engine_of(engine)))


def _records(df: pd.DataFrame) -> list[dict]:
    """NaN and ±inf aren't valid JSON and 500 the endpoint on serialization."""
    return df.replace([np.inf, -np.inf], np.nan).replace({np.nan: None}).to_dict(orient="records")


def _pearson(x: pd.Series, y: pd.Series) -> dict | None:
    """Pearson r with a two-sided p-value, or nulls when it's undefined.

    Guarded rather than assumed: with 14 teams a single season is a small
    sample, and a degenerate one (every team identical, or fewer than three
    points) would otherwise raise instead of reporting "no signal".
    """
    ok = x.notna() & y.notna()
    x, y = x[ok], y[ok]
    if len(x) < 3 or x.nunique() < 2 or y.nunique() < 2:
        return {"r": None, "p": None, "n": int(len(x))}
    r, p = stats.pearsonr(x, y)
    return {"r": float(r), "p": float(p), "n": int(len(x))}


def _accuracy(actual: pd.Series, predicted: pd.Series) -> dict:
    """RMSE / MAE / R² / signed bias of a projection against what happened."""
    ok = actual.notna() & predicted.notna()
    a, p = actual[ok], predicted[ok]
    if len(a) < 2:
        return {"n": int(len(a)), "rmse": None, "mae": None, "r2": None, "bias": None}
    err = p - a
    ss_res = float((err ** 2).sum())
    ss_tot = float(((a - a.mean()) ** 2).sum())
    return {
        "n": int(len(a)),
        "rmse": float((err ** 2).mean() ** 0.5),
        "mae": float(err.abs().mean()),
        # Negative R² is meaningful here (the projection did worse than always
        # guessing the mean), so it is reported rather than clipped at zero.
        "r2": float(1 - ss_res / ss_tot) if ss_tot > 0 else None,
        "bias": float(err.mean()),
    }


# ---------------------------------------------------------------------------
# NB01 / NB02 — what the dataset actually is
# ---------------------------------------------------------------------------

def dataset_provenance(engine) -> dict:
    """Row counts and season coverage per table, so the reader can size the evidence.

    Deliberately first in the payload: every claim further down rests on this,
    and "r = -0.75" reads very differently against 14 teams in one season than
    against 14,000 rows.
    """
    specs = [
        ("players_stats", "Player-seasons", "Actual and projected scoring, per player per season"),
        ("drafts", "Draft picks", "Every pick made in this league, with round and slot"),
        ("average_draft_position", "ADP rows", "Consensus market ranking, from 2022 onward"),
        ("next_season_projections", "Live candidates", "The pool the draft board ranks from"),
        ("teams", "Team-seasons", "Final standings and points for/against"),
    ]
    out = []
    for table, label, blurb in specs:
        if not _has_table(engine, table):
            continue
        row = pd.read_sql(text(f"SELECT COUNT(*) AS n FROM {table}"), _engine_of(engine))
        entry = {"table": table, "label": label, "blurb": blurb, "rows": int(row["n"].iloc[0])}
        try:
            yrs = pd.read_sql(text(f"SELECT DISTINCT year FROM {table} ORDER BY year"), _engine_of(engine))
            years = [int(y) for y in yrs["year"].dropna()]
            entry["years"] = f"{years[0]}–{years[-1]}" if years else None
        except Exception:
            entry["years"] = None
        out.append(entry)
    return {"tables": out, "broken_projection_years": list(BROKEN_PROJECTION_YEARS)}


# ---------------------------------------------------------------------------
# NB03 — retrospective: replacement level, draft value, who drafted best
# ---------------------------------------------------------------------------

def load_draft_season(engine, year: int) -> pd.DataFrame:
    """Drafted players for `year`, with VORP on the live board's scale.

    Four sections need this same frame, so it's memoized on the request context
    when there is one. A copy is handed out rather than the cached frame itself
    — none of the current callers mutate it, but a shared DataFrame is the kind
    of landmine that only goes off later, and copying ~1,200 rows is free.
    """
    ctx = _ctx(engine)
    if year in ctx._draft:
        return ctx._draft[year].copy()
    if not all(_has_table(ctx, t) for t in _REQUIRED_DRAFT_TABLES) or not ctx.league_id:
        ctx._draft[year] = pd.DataFrame()
        return pd.DataFrame()
    df = pd.read_sql(text(_DRAFT_SEASON_SQL), _engine_of(engine),
                     params={"year": year, "league_id": ctx.league_id})
    if not df.empty:
        df["position"] = df["position"].map(normalize_position)
        df = df[df["position"].notna()]
        baselines = compute_baselines(df, teams=TEAMS, roster_needs=ctx.roster_need, value_col="points")
        df = add_vorp(df, baselines, value_col="points", out_col="vorp")
    ctx._draft[year] = df
    return df.copy()


def replacement_levels(engine, year: int) -> dict:
    """What "replacement level" actually resolved to, per position, last season.

    The single most load-bearing number in the project and the one most often
    hand-waved: VORP is meaningless without saying what it's *over*. Derived
    from league settings (teams × starters, plus a share of FLEX) rather than a
    hand-picked rank cutoff, which is what earlier versions used.
    """
    ctx = _ctx(engine)
    if not _has_columns(ctx, "players_stats", _PROJECTION_COLUMNS) or not ctx.league_id:
        return {"year": year, "positions": []}
    df = pd.read_sql(text(_PROJECTION_ACTUALS_SQL), _engine_of(engine),
                     params={"year": year, "league_id": ctx.league_id})
    if df.empty:
        return {"year": year, "positions": []}
    df["position"] = df["position"].map(normalize_position)
    df = df[df["position"].isin(set(POSITIONS))]
    baselines = compute_baselines(df, teams=TEAMS, roster_needs=ctx.roster_need, value_col="actual_points")

    rows = []
    for pos, grp in df.groupby("position"):
        n = starters_needed(pos, teams=TEAMS, roster_needs=ctx.roster_need)
        rows.append({
            "position": pos,
            "starters_league_wide": n,
            "baseline_points": float(baselines.get(pos, 0.0)),
            "best_points": float(grp["actual_points"].max()),
            "pool": int(len(grp)),
        })
    rows.sort(key=lambda r: -r["baseline_points"])
    return {"year": year, "teams": TEAMS, "roster_needs": ctx.roster_need, "positions": rows}


def draft_value_by_round(engine, year: int) -> list[dict]:
    """Average VORP returned by each round — how fast draft capital decays."""
    df = load_draft_season(engine, year)
    if df.empty or "round" not in df.columns:
        return []
    by_round = (
        df.dropna(subset=["round"])
        .groupby("round")
        .agg(avg_vorp=("vorp", "mean"), picks=("vorp", "size"),
             hit_rate=("vorp", lambda s: float((s > 0).mean())))
        .reset_index()
        .sort_values("round")
    )
    by_round["round"] = by_round["round"].astype(int)
    return _records(by_round)


def draft_performance(engine, year: int) -> dict:
    """Average VORP of each team's drafted players against how they finished.

    The correlation is signed against `final_standing`, where 1 = champion, so a
    *negative* r is the intuitive result: drafting better went with finishing
    nearer the top. The frontend restates this in words rather than making the
    reader remember the sign convention.
    """
    df = load_draft_season(engine, year)
    if df.empty:
        return {"year": year, "teams": [], "correlation": None}

    by_team = (
        df.groupby("team_name")
        .agg(avg_vorp=("vorp", "mean"), total_vorp=("vorp", "sum"),
             picks=("vorp", "size"), final_standing=("final_standing", "first"))
        .reset_index()
        .sort_values("avg_vorp", ascending=False)
    )
    return {
        "year": year,
        "teams": _records(by_team),
        "correlation": _pearson(by_team["avg_vorp"], by_team["final_standing"]),
    }


def career_performance(engine, seasons: list[int]) -> dict:
    """Every manager's draft record across all stored seasons.

    The page could only ever ask "who drafted best in 2025?", which is a
    question about one year of luck as much as skill. With six seasons stored,
    "who is actually good at this" is both answerable and the thing people
    argue about.

    **Grouped on `team_id`, never `team_name`.** Fourteen franchises in this
    league have used eighteen names. Two of them collide on a space -
    "Gerald Pea's Football Team" (id 11, later "B Mac") and "GeraldPea's
    Football Team" (id 12, formerly "Jean Machine") are different people - so
    grouping by name would merge two managers and split two others, inventing
    four who never existed. `src/biases.py` already documents this hazard for
    the manager fit; it applies identically here.

    Each franchise is labeled with its most recent name, since that is who
    everyone in the league calls them now.
    """
    frames = []
    for year in seasons:
        df = load_draft_season(engine, year)
        if not df.empty:
            frames.append(df.assign(year=year))
    if not frames:
        return {"seasons": [], "managers": []}

    allp = pd.concat(frames, ignore_index=True)
    if "team_id" not in allp.columns:      # pre-migration database
        return {"seasons": [], "managers": []}

    # Per manager-season first, so a season counts once however many picks it had.
    per_season = (
        allp.groupby(["team_id", "year"])
        .agg(avg_vorp=("vorp", "mean"), total_vorp=("vorp", "sum"),
             picks=("vorp", "size"), final_standing=("final_standing", "first"),
             team_name=("team_name", "first"))
        .reset_index()
    )

    rows = []
    for team_id, g in per_season.groupby("team_id"):
        g = g.sort_values("year")
        best = g.loc[g["avg_vorp"].idxmax()]
        worst = g.loc[g["avg_vorp"].idxmin()]
        rows.append({
            "team_id": int(team_id),
            # Latest name wins; earlier ones are kept so the UI can say
            # "formerly ..." rather than silently renaming someone's history.
            "team_name": g["team_name"].iloc[-1],
            "former_names": [n for n in dict.fromkeys(g["team_name"]) if n != g["team_name"].iloc[-1]],
            "seasons_n": int(g["year"].nunique()),
            "avg_vorp": float(g["avg_vorp"].mean()),
            "total_vorp": float(g["total_vorp"].sum()),
            "picks": int(g["picks"].sum()),
            "titles": int((g["final_standing"] == 1).sum()),
            "best_year": int(best["year"]), "best_vorp": float(best["avg_vorp"]),
            "worst_year": int(worst["year"]), "worst_vorp": float(worst["avg_vorp"]),
            "avg_finish": float(g["final_standing"].mean()),
            # Per season, for the sparkline: signed against this manager's own
            # average, so it reads as "a good year for them" not "a good year".
            "by_season": [{"year": int(r.year), "avg_vorp": float(r.avg_vorp),
                           "final_standing": int(r.final_standing)}
                          for r in g.itertuples()],
        })

    rows.sort(key=lambda r: r["avg_vorp"], reverse=True)
    return {"seasons": sorted({int(y) for y in allp["year"].unique()}), "managers": rows}


def best_and_worst_picks(engine, seasons: list[int], n: int = 8) -> dict:
    """The best and worst picks in the league's whole history.

    `steals_and_reaches` answers this for one season. Asked of every season at
    once it becomes the thing people actually argue about at the next draft -
    the pick someone is still being mocked for, and the one they will not stop
    bringing up.

    "Best" is value against where they went, not raw points: a first overall
    pick scoring well is the system working, not a steal.
    """
    frames = []
    for year in seasons:
        df = load_draft_season(engine, year)
        if not df.empty:
            frames.append(df.assign(year=year))
    if not frames:
        return {"best": [], "worst": []}

    allp = pd.concat(frames, ignore_index=True)
    allp = allp[allp["adp"].notna() & (allp["adp"] < NO_ADP_SENTINEL)]
    if allp.empty:
        return {"best": [], "worst": []}

    cols = ["player_name", "team_name", "position", "year", "pick", "adp",
            "draft_delta", "points", "vorp"]
    # Best: fell past the market and delivered anyway. Worst: taken early and
    # didn't. Both keyed on VORP, so "worst" is a genuinely costly pick rather
    # than a late flier that didn't work out.
    best = allp[allp["draft_delta"] > 0].nlargest(n, "vorp")
    worst = allp[allp["draft_delta"] < 0].nsmallest(n, "vorp")
    return {"best": _records(best[cols]), "worst": _records(worst[cols])}


_LUCK_SQL = """
SELECT team_id, team_name, year, final_standing, points_for, points_against,
       wins, losses
FROM teams
WHERE league_id = :league_id AND points_for IS NOT NULL AND points_for > 0
"""


def luck(engine, seasons: list[int]) -> dict:
    """Who finished better than their scoring deserved, and who finished worse.

    Fantasy standings are decided by weekly head-to-head, so the team that
    scores the most points routinely doesn't win. Ranking each season by points
    and comparing that to the actual finish separates "good team" from "good
    schedule" - and it is the single most reliable argument starter in any
    league.

    Averaged across seasons so one unlucky year doesn't define anyone.
    """
    ctx = _ctx(engine)
    if not _has_table(ctx, "teams") or not ctx.league_id:
        return {"teams": [], "seasons": []}
    df = pd.read_sql(text(_LUCK_SQL), _engine_of(engine),
                     params={"league_id": ctx.league_id})
    if df.empty:
        return {"teams": [], "seasons": []}
    if seasons:
        df = df[df["year"].isin(seasons)]
    if df.empty:
        return {"teams": [], "seasons": []}

    # Rank within each season: 1 = most points scored that year.
    df["points_rank"] = df.groupby("year")["points_for"].rank(ascending=False, method="min")
    # Positive = finished better than their scoring rank. Same sign convention
    # as `expectations`, so "+3" reads the same way on both.
    df["luck"] = df["points_rank"] - df["final_standing"]

    by_team = (
        df.groupby("team_id")
        .agg(team_name=("team_name", "last"), seasons_n=("year", "nunique"),
             avg_points_rank=("points_rank", "mean"),
             avg_finish=("final_standing", "mean"),
             luck=("luck", "mean"), points_for=("points_for", "mean"))
        .reset_index()
        .sort_values("luck", ascending=False)
    )
    return {"teams": _records(by_team),
            "seasons": sorted(int(y) for y in df["year"].unique())}


_EXPECTATIONS_SQL = """
SELECT team_id, team_name, draft_projected_rank, final_standing,
       wins, losses, points_for
FROM teams
WHERE league_id = :league_id AND year = :year
"""


def expectations(engine, year: int) -> dict:
    """ESPN's own preseason ranking against where each team actually finished.

    A projection nobody grades, sitting unused in the `teams` table. It is a
    different question from "did they draft well": you can draft badly and
    still beat a low bar.

    `draft_projected_rank` is missing for 12 of 76 rows in this database, so a
    team without one is reported as unknown rather than dropped or - much
    worse - treated as rank 0, which would read as the best projection anyone
    ever had.
    """
    ctx = _ctx(engine)
    if not _has_table(ctx, "teams") or not ctx.league_id:
        return {"year": year, "teams": [], "missing": 0}
    df = pd.read_sql(text(_EXPECTATIONS_SQL), _engine_of(engine),
                     params={"year": year, "league_id": ctx.league_id})
    if df.empty:
        return {"year": year, "teams": [], "missing": 0}

    df["draft_projected_rank"] = pd.to_numeric(df["draft_projected_rank"], errors="coerce")
    df.loc[df["draft_projected_rank"] <= 0, "draft_projected_rank"] = pd.NA
    missing = int(df["draft_projected_rank"].isna().sum())
    # Positive = finished better than projected. Sign chosen so the number
    # reads the way people say it: "+6" is "six places better than expected".
    df["beat_by"] = df["draft_projected_rank"] - df["final_standing"]
    df = df.sort_values(["beat_by", "final_standing"], ascending=[False, True],
                        na_position="last")
    return {"year": year, "teams": _records(df), "missing": missing}


def steals_and_reaches(engine, year: int, n: int = 8) -> dict:
    """Where the market was most wrong, in both directions.

    `draft_delta` is pick minus ADP, so positive means they lasted longer than
    the market expected. A "steal" is a late pick who returned high VORP; a
    "reach" is an early pick who didn't.
    """
    df = load_draft_season(engine, year)
    if df.empty:
        return {"steals": [], "reaches": []}
    cols = ["player_name", "team_name", "position", "pick", "adp", "draft_delta", "points", "vorp"]
    return {
        "steals": _records(df[df["draft_delta"] > 0].nlargest(n, "vorp")[cols]),
        "reaches": _records(df[df["draft_delta"] < 0].nsmallest(n, "vorp")[cols]),
    }


# ---------------------------------------------------------------------------
# NB05 — how good were the projections, and do they beat the market
# ---------------------------------------------------------------------------

def _projection_actuals(engine, years: list[int]) -> pd.DataFrame:
    """Projected vs. actual VORP per player-season, on a common scale.

    Baselines are recomputed per season and separately for the projected and
    actual distributions, because replacement level shifts year to year and
    grading a projection against the *actual* baseline would confuse "the
    projection was wrong" with "the league scored more that year".

    Memoized on the request context: `projection_accuracy` and `adp_benchmark`
    both need this over the same year list, and it is the single most expensive
    thing in the payload — twelve baseline computations across six seasons.
    """
    ctx = _ctx(engine)
    if not ctx.league_id:
        return pd.DataFrame()
    key = tuple(years)
    if key in ctx._actuals:
        return ctx._actuals[key].copy()
    frames = []
    for year in years:
        df = pd.read_sql(text(_PROJECTION_ACTUALS_SQL), _engine_of(engine),
                         params={"year": year, "league_id": ctx.league_id})
        if df.empty:
            continue
        df["position"] = df["position"].map(normalize_position)
        df = df[df["position"].isin(set(POSITIONS))]
        if df.empty:
            continue
        proj_base = compute_baselines(df, teams=TEAMS, roster_needs=ctx.roster_need, value_col="projected_points")
        act_base = compute_baselines(df, teams=TEAMS, roster_needs=ctx.roster_need, value_col="actual_points")
        df = add_vorp(df, proj_base, value_col="projected_points", out_col="proj_vorp").drop(columns="baseline")
        df = add_vorp(df, act_base, value_col="actual_points", out_col="actual_vorp").drop(columns="baseline")
        df["year"] = year
        frames.append(df)
    out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    ctx._actuals[key] = out
    return out.copy()


def projection_accuracy(engine, through: int) -> dict:
    """How accurate the preseason projections turned out to be, per season.

    This sets the ceiling on the whole tool: the board leans on these
    projections, so it can never be more right than they are.
    """
    if not _has_columns(engine, "players_stats", _PROJECTION_COLUMNS):
        return {"by_season": [], "overall": None, "years": []}
    years = [y for y in range(2020, through + 1) if y not in BROKEN_PROJECTION_YEARS]
    df = _projection_actuals(engine, years)
    if df.empty:
        return {"by_season": [], "overall": None, "years": years}

    by_season = [
        {"year": int(y), **_accuracy(g["actual_vorp"], g["proj_vorp"])}
        for y, g in df.groupby("year")
    ]
    return {
        "years": years,
        "by_season": by_season,
        "overall": _accuracy(df["actual_vorp"], df["proj_vorp"]),
        "excluded": list(BROKEN_PROJECTION_YEARS),
    }


def adp_benchmark(engine, through: int) -> dict:
    """Do the projections beat the market — and where it counts?

    Spearman rank correlation, not RMSE: ADP is a pick number and VORP is points
    above replacement, so an error metric between them is meaningless. Drafting
    is an ordering problem anyway — you never need to know a player will score
    214.7, only that he should go before the next guy.

    Reported over three scopes because the full pool flatters every ranker:
    separating stars from deep-bench players is easy and nobody agonises over it
    on draft day. The top-50 scope asks the decision-relevant question.

    One caveat the UI repeats: ADP is not independent of these projections —
    many of the people setting it are looking at the same numbers. These are
    correlated rankers, not a controlled comparison.
    """
    if not (_has_columns(engine, "players_stats", _PROJECTION_COLUMNS)
            and _has_table(engine, "average_draft_position")):
        return {"scopes": [], "years": []}
    years = [y for y in range(2020, through + 1) if y not in BROKEN_PROJECTION_YEARS]
    df = _projection_actuals(engine, years)
    if df.empty:
        return {"scopes": [], "years": []}

    adp = pd.read_sql(
        text("SELECT player_id, year, avg AS adp FROM average_draft_position WHERE avg IS NOT NULL"),
        _engine_of(engine),
    )
    adp["player_id"] = pd.to_numeric(adp["player_id"], errors="coerce")
    adp = adp.dropna(subset=["player_id"])
    adp["player_id"] = adp["player_id"].astype("int64")

    bench = df.merge(adp, on=["player_id", "year"], how="inner")
    if bench.empty:
        return {"scopes": [], "years": []}
    # Negated so higher = better for every ranker, making them directly comparable.
    bench["market"] = -bench["adp"]

    def spearman_rows(frame, label):
        rows = []
        for year, grp in frame.groupby("year"):
            if len(grp) < 3:
                continue
            rows.append({
                "scope": label, "year": int(year), "n": int(len(grp)),
                "projection": float(grp["proj_vorp"].corr(grp["actual_vorp"], method="spearman")),
                "market": float(grp["market"].corr(grp["actual_vorp"], method="spearman")),
            })
        return rows

    scopes = [("All players", bench)]
    for top_n in (100, 50):
        top = bench.sort_values("adp").groupby("year", group_keys=False).head(top_n)
        scopes.append((f"Top {top_n} by ADP", top))

    out = []
    for label, frame in scopes:
        rows = spearman_rows(frame, label)
        if not rows:
            continue
        proj = [r["projection"] for r in rows]
        mkt = [r["market"] for r in rows]
        out.append({
            "scope": label,
            "by_year": rows,
            "mean_projection": float(np.mean(proj)),
            "mean_market": float(np.mean(mkt)),
            "seasons_won": int(sum(p > m for p, m in zip(proj, mkt))),
            "seasons": len(rows),
        })
    return {"scopes": out, "years": sorted(bench["year"].unique().tolist())}


# ---------------------------------------------------------------------------
# Bench depth (feeds the live need multiplier)
# ---------------------------------------------------------------------------

def league_bias(engine) -> dict:
    """How this league drafts differently from the national market.

    Pure reads of what `notebooks/compute_league_bias.py` persisted — no
    computation here. The fit involves permutation tests over 32 NFL teams and
    14 managers, which is not something to run inside a GET.

    `applied` records which families actually move the board, because the
    distinction matters to a reader: position and NFL-team effects change the
    pick estimate, while manager and per-player effects are measured and shown
    but deliberately not applied (see `src/biases.py::apply_league_bias`).
    """
    return {
        "position": _read_table(engine, "league_bias_position"),
        "position_season": _read_table(engine, "league_bias_position_season", order_by="position, year"),
        "pro_team": _read_table(engine, "league_bias_proteam"),
        "proteam_season": _read_table(engine, "league_bias_proteam_season", order_by="pro_team, year"),
        "manager": _read_table(engine, "league_bias_manager"),
        "player": _read_table(engine, "league_bias_player"),
        "meta": (_read_table(engine, "league_bias_meta") or [None])[0],
        "applied": ["position", "pro_team"],
    }


def bench_depth(engine) -> dict:
    """Why a backup RB is worth roughly twenty times a backup DST."""
    rows = _read_table(engine, "position_availability", order_by="missed_game_rate DESC")
    rates = {r["position"]: r["missed_game_rate"] for r in rows} if rows else None
    needs = depth_needs(teams=TEAMS, roster_needs=ROSTER_NEEDS, missed_game_rate=rates)
    for r in rows:
        r["depth_value"] = needs.get(r["position"])
    return {"availability": rows, "depth_value": needs}


# ---------------------------------------------------------------------------
# payload
# ---------------------------------------------------------------------------

def league_analysis(engine, year: int | None = None, league_id: str | None = None,
                    roster_need: dict | None = None) -> dict:
    """Everything the Analysis tab renders, in one round-trip.

    One request context is built here and threaded through every section, so the
    expensive frames each section would otherwise load for itself are read once.

    Scoped to one league. When the caller doesn't name one, fall back to the
    only league with history - which keeps single-league databases behaving
    exactly as before. With several, refusing to guess is the honest answer:
    picking arbitrarily would label one league's drafters with another's habits.
    """
    if league_id is None:
        stored = known_leagues(engine if not isinstance(engine, _Ctx) else engine.engine)
        league_id = stored[0] if len(stored) == 1 else None
    engine = _ctx(engine, league_id, roster_need)
    seasons = (seasons_for_league(engine.engine, league_id) if league_id else [])
    # The retrospective sections describe one season, and it has to be a season
    # this league actually drafted in. Defaulting to the calendar's current
    # season would show an empty page for a league that skipped a year or ran
    # its draft offline - Sigmas' 2025 draft was never completed in ESPN, so
    # its most recent real draft is 2024.
    if year is None or (seasons and year not in seasons):
        year = seasons[-1] if seasons else (year or CURRENT_SEASON)
    return {
        "year": year,
        "league_id": league_id,
        # Which seasons of this league's own history are stored. Drives the
        # "needs prior seasons" gate: an empty list means the league-specific
        # half of the page has nothing to say.
        "seasons": seasons,
        "has_history": bool(seasons),
        "teams_in_league": TEAMS,
        "roster_needs": engine.roster_need,
        "dataset": dataset_provenance(engine),
        "replacement_levels": replacement_levels(engine, year),
        "draft_value_by_round": draft_value_by_round(engine, year),
        "draft_performance": draft_performance(engine, year),
        # Across every stored season, not just the selected one. This is the
        # question the page now opens with.
        "career_performance": career_performance(engine, seasons),
        "expectations": expectations(engine, year),
        "best_and_worst_picks": best_and_worst_picks(engine, seasons),
        "luck": luck(engine, seasons),
        "steals_and_reaches": steals_and_reaches(engine, year),
        "projection_accuracy": projection_accuracy(engine, year),
        "adp_benchmark": adp_benchmark(engine, year),
        "bench_depth": bench_depth(engine),
        "league_bias": league_bias(engine),
        # Which league the persisted tables below were fitted on. They are
        # NOT recomputed per league: projection reliability and the regression
        # diagnostics come from whichever league's history was processed. When
        # that isn't the league being viewed, the UI labels them as a general
        # reference rather than presenting them as this league's own numbers.
        "reference_league_id": (
            (_read_table(engine, "league_bias_meta") or [{}])[0].get("league_id")
        ),
        # Persisted notebook outputs. Empty lists when that notebook hasn't been
        # run, which the UI renders as "not available yet" rather than as zeros.
        "position_reliability": _read_table(engine, "position_reliability", order_by="r2 DESC"),
        "rookie_reliability": _read_table(engine, "rookie_reliability"),
        "model_report": _read_table(engine, "model_report", order_by="cv_rmse ASC"),
        "model_ablation": _read_table(engine, "model_ablation", order_by="step ASC"),
        "feature_vif": _read_table(engine, "feature_vif", order_by="vif DESC"),
    }


