"""Schema changes the app applies to an existing database on its own.

The database is built by `to_sql`, which has no notion of migrations: it either
leaves a table alone or drops and recreates it. That was fine while everything
described one league, but `drafts`, `teams` and `players_stats` are all
league-scoped and had no column saying which league they came from.

Rather than force a full re-pull, these are additive and idempotent — add the
column if it's missing, backfill the rows that predate it, and index the shape
the analysis actually queries. Safe to run on every startup.

Why `players_stats` is league-scoped, which is not obvious: it holds *fantasy*
points, and scoring settings differ between leagues. McFL has 37 scoring items
where the user's other two leagues have 46, so the same player-season is worth
different amounts in each. Sharing those rows across leagues would silently
report one league's scoring as another's.

`average_draft_position` is deliberately NOT scoped: it's the national consensus
market, identical whatever league you're in.
"""

from __future__ import annotations

import logging

from sqlalchemy import text

log = logging.getLogger(__name__)

# Tables that describe one league's own history, and therefore need to say
# which league. Order matters only for readability.
LEAGUE_SCOPED_TABLES = ("drafts", "teams", "players_stats")

# The fitted-bias tables. `league_bias_meta` already carries a league_id; these
# six never did, and `persist_league_bias` wrote them with
# `if_exists="replace"` - so fitting one league dropped every other league's
# rows, and served their manager names to the wrong people.
BIAS_TABLES = (
    "league_bias_position",
    "league_bias_proteam",
    "league_bias_manager",
    "league_bias_player",
    "league_bias_position_season",
    "league_bias_proteam_season",
)

LEAGUE_INDEXES = [
    ("idx_drafts_league_year", "drafts", "drafts(league_id, year)"),
    ("idx_teams_league_year", "teams", "teams(league_id, year)"),
    ("idx_stats_league_year", "players_stats", "players_stats(league_id, year)"),
]


def _table_exists(conn, name: str) -> bool:
    q = text("SELECT name FROM sqlite_master WHERE type='table' AND name=:n")
    return conn.execute(q, {"n": name}).first() is not None


def _columns(conn, table: str) -> set:
    return {row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))}


def add_league_id(engine, default_league_id: str | None = None) -> dict:
    """Give the league-scoped tables a `league_id`, backfilling existing rows.

    Everything already in the database was collected from a single configured
    league, so `default_league_id` (normally `LEAGUE_ID` from the environment)
    is the honest label for it. Without a default the column is still added but
    left null, which the analysis reads as "unknown league" rather than
    silently attributing the rows to whichever league is being viewed.
    """
    added, backfilled = [], {}
    with engine.begin() as conn:
        for table in LEAGUE_SCOPED_TABLES:
            if not _table_exists(conn, table):
                continue
            if "league_id" not in _columns(conn, table):
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN league_id TEXT"))
                added.append(table)
            if default_league_id:
                result = conn.execute(
                    text(f"UPDATE {table} SET league_id = :lid WHERE league_id IS NULL"),
                    {"lid": str(default_league_id)},
                )
                if result.rowcount:
                    backfilled[table] = result.rowcount

        for name, table, body in LEAGUE_INDEXES:
            if _table_exists(conn, table) and "league_id" in _columns(conn, table):
                conn.execute(text(f"CREATE INDEX IF NOT EXISTS {name} ON {body}"))

    if added or backfilled:
        log.info("league_id added to %s, backfilled %s", added or "nothing", backfilled or "nothing")
    return {"added": added, "backfilled": backfilled}


def known_leagues(engine) -> list[str]:
    """League ids that actually have history stored, for the analysis gate."""
    with engine.connect() as conn:
        if not _table_exists(conn, "drafts") or "league_id" not in _columns(conn, "drafts"):
            return []
        rows = conn.execute(text(
            "SELECT DISTINCT league_id FROM drafts WHERE league_id IS NOT NULL"
        ))
        return [str(r[0]) for r in rows]


def seasons_for_league(engine, league_id: str) -> list[int]:
    """Which seasons of draft history are stored for a league.

    Drives the Analysis tab's "this league has 2021-2025" line, and the
    decision about whether the league-specific half of the page can be shown
    at all.
    """
    with engine.connect() as conn:
        if not _table_exists(conn, "drafts") or "league_id" not in _columns(conn, "drafts"):
            return []
        rows = conn.execute(
            text("SELECT DISTINCT year FROM drafts WHERE league_id = :lid ORDER BY year"),
            {"lid": str(league_id)},
        )
        return [int(r[0]) for r in rows if r[0] is not None]


def add_bias_league_id(engine, default_league_id: str | None = None) -> dict:
    """Give the fitted-bias tables a league_id, labeling existing rows honestly.

    The default comes from `league_bias_meta.league_id` rather than from the
    environment: the fit itself recorded which league it measured, which is the
    only trustworthy source. They genuinely differ here - the last fit on this
    database was a second league, not the one in LEAGUE_ID.

    Idempotent, and called from `persist_league_bias` as well as at boot,
    because `to_sql(if_exists="replace")` *drops and recreates* a table. A
    migration that ran only at startup would be silently undone by the next
    write, and the DELETE that scopes the write would then have no column to
    filter on.
    """
    added, backfilled = [], {}
    with engine.begin() as conn:
        default = default_league_id
        if default is None and _table_exists(conn, "league_bias_meta"):
            row = conn.execute(
                text("SELECT league_id FROM league_bias_meta "
                     "WHERE league_id IS NOT NULL LIMIT 1")).first()
            default = str(row[0]).strip() if row and row[0] is not None else None

        for table in BIAS_TABLES:
            if not _table_exists(conn, table):
                continue
            if "league_id" not in _columns(conn, table):
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN league_id TEXT"))
                added.append(table)
            if default:
                res = conn.execute(
                    text(f"UPDATE {table} SET league_id = :lid WHERE league_id IS NULL"),
                    {"lid": default})
                if res.rowcount:
                    backfilled[table] = res.rowcount
            conn.execute(text(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_league "
                f"ON {table}(league_id)"))
    return {"added": added, "backfilled": backfilled}


def fitted_leagues(engine) -> list[str]:
    """Every league that has a stored bias fit."""
    with engine.begin() as conn:
        if not _table_exists(conn, "league_bias_meta"):
            return []
        if "league_id" not in _columns(conn, "league_bias_meta"):
            return []
        rows = conn.execute(text(
            "SELECT DISTINCT league_id FROM league_bias_meta "
            "WHERE league_id IS NOT NULL ORDER BY league_id"))
        return [str(r[0]).strip() for r in rows if r[0] is not None]
