"""Indexes for the analytical queries the live tool runs on every pick.

The database was built entirely by `to_sql`, which creates no indexes at all, so
every join was a full scan. The one that mattered is the ADP join in
`src/recommender.py::load_candidates` — it runs once per `/recommend` and four
times per `/analysis`, and without an index SQLite scans ~2,300 ADP rows and
applies the `CAST` row by row for each of ~1,026 candidates. Measured on the
real database: **57ms → 1.4ms**, taking `/recommend` from 166ms to ~110ms.

**Sharp edge.** `idx_adp_year_playerid` is an *expression* index. SQLite only
uses it when a query spells the expression textually identically to the index
definition — `CAST(x.player_id AS INTEGER)`. Change the spelling at either call
site (`src/recommender.py::load_candidates`, `src/analysis.py::_DRAFT_SEASON_SQL`)
and the index silently stops being used, with no error and no test failure. Both
call sites carry a comment pointing here.

**Where this must be called from.** Not NB02's schema cell: that cell drops the
tables, and the `to_sql(if_exists="replace")` calls after it drop and recreate
each table *along with its indexes*. Index creation has to come after the last
write, so it lives in NB02's final cell, in `notebooks/create_indexes.py`, and
on API startup.
"""

from __future__ import annotations

from sqlalchemy import text

# (index name, table it needs, full CREATE INDEX body)
INDEXES: list[tuple[str, str, str]] = [
    # The hot one. See module docstring before changing the CAST spelling.
    ("idx_adp_year_playerid", "average_draft_position",
     "average_draft_position(year, CAST(player_id AS INTEGER))"),
    # Prior-season production, joined per candidate by the recommender and by
    # every accuracy query in src/analysis.py.
    ("idx_stats_playerid_year", "players_stats", "players_stats(player_id, year)"),
    # Retrospective analysis filters drafts by season.
    ("idx_drafts_year_playerid", "drafts", "drafts(year, player_id)"),
    ("idx_teams_teamid_year", "teams", "teams(team_id, year)"),
    ("idx_nsp_year", "next_season_projections", "next_season_projections(year)"),
]


def _table_exists(conn, name: str) -> bool:
    q = text("SELECT name FROM sqlite_master WHERE type='table' AND name=:n")
    return conn.execute(q, {"n": name}).first() is not None


def ensure_indexes(engine, verbose: bool = False) -> list[str]:
    """Create any missing index. Returns the names that now exist.

    Each index is guarded by a table-existence check because `CREATE INDEX`
    raises on a missing table, and a partially built database — or the test
    fixture, which has no `teams` or `players` — is a legitimate state.
    """
    created = []
    with engine.begin() as conn:
        for name, table, body in INDEXES:
            if not _table_exists(conn, table):
                continue
            conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {body}"))
            created.append(name)
            if verbose:
                print(f"  {name} on {table}")
    return created
