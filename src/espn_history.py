"""Pull one league's completed-season history, so it can be analyzed.

The Analysis tab needs three things per league: who drafted whom, how each team
finished, and what every player actually scored *under that league's scoring
settings*. Only the first two are obviously league-specific — the third is the
one that catches people out. McFL has 37 scoring items where the user's other
leagues have 46, so the same player-season is worth different amounts in each,
and sharing those rows across leagues would report one league's scoring as
another's.

Deliberately does not import `espn_api`, for the same reason
`src/espn_draft.py` doesn't: that package's `ESPNAccessDenied` formats
`espn_s2` and `swid` into its message, and this code runs inside a job whose
errors are surfaced to the user. Raw requests, with the same constant-message
error mapping.

One request per season per view. Player stats come from `kona_player_info`,
which returns the whole season in a single response (~1,100 players, ~2MB) -
far cheaper than walking 17 weeks of box scores, which is what the original
notebook did.
"""

from __future__ import annotations

import json
import logging

import pandas as pd
import requests
from sqlalchemy import text

from src.espn_draft import (
    AUTH_MESSAGE,
    BASE,
    UNAVAILABLE_MESSAGE,
    EspnAuthError,
    EspnCredentials,
    EspnUnavailable,
    parse_draft_detail,
    parse_settings,
)
from src.scoring import normalize_position, position_from_eligible_slots

log = logging.getLogger(__name__)

# ESPN stat ids for the usage numbers the player-detail panel shows. Only the
# three the UI actually reads - the payload carries hundreds.
STAT_IDS = {"0": "actual_passingAttempts", "23": "actual_rushingAttempts",
            "58": "actual_receivingTargets"}

# A season's worth of players in one call. The sort is required by ESPN; the
# limit is comfortably above any league's player universe.
PLAYER_FILTER = {"players": {"limit": 1500,
                             "sortPercOwned": {"sortPriority": 1, "sortAsc": False}}}

# statSourceId 0 = what actually happened, 1 = the preseason projection.
# statSplitTypeId 0 = the season total (as opposed to a single week).
ACTUAL, PROJECTED, SEASON_TOTAL = 0, 1, 0


def _fetch(creds: EspnCredentials, url: str, params: dict,
           headers: dict | None) -> requests.Response:
    try:
        return requests.get(
            url,
            params=params,
            cookies={"SWID": creds.swid, "espn_s2": creds.espn_s2},
            headers=headers or {},
            timeout=45,
        )
    except requests.RequestException:
        # `from None`: the original can carry the request, and the request
        # headers are the cookies.
        raise EspnUnavailable(UNAVAILABLE_MESSAGE) from None


def _get(creds: EspnCredentials, league_id: str, year: int, view: str,
         headers: dict | None = None) -> dict:
    """One view of one season, from whichever endpoint still serves it.

    ESPN keeps a league's recent seasons under `/seasons/{year}/...` and moves
    older ones to `/leagueHistory/{id}?seasonId={year}`. The two are not
    interchangeable and the old one 404s on the new path, so asking only the
    modern endpoint reports a league as having started whenever ESPN last
    migrated it - which is indistinguishable, from here, from the league
    genuinely not existing yet.

    The fallback is only tried on a 404, so a league with nothing that year
    still costs two cheap requests and every season that works costs one.
    """
    resp = _fetch(creds, f"{BASE}/seasons/{year}/segments/0/leagues/{league_id}",
                  {"view": view}, headers)
    if resp.status_code in (401, 403):
        raise EspnAuthError(AUTH_MESSAGE)
    if resp.status_code == 404:
        resp = _fetch(creds, f"{BASE}/leagueHistory/{league_id}",
                      {"view": view, "seasonId": year}, headers)
        if resp.status_code in (401, 403):
            raise EspnAuthError(AUTH_MESSAGE)
    if resp.status_code == 404:
        # A season before the league existed. Not an error - the caller skips it.
        return {}
    if resp.status_code >= 400:
        raise EspnUnavailable(UNAVAILABLE_MESSAGE)
    try:
        payload = resp.json()
    except ValueError:
        raise EspnUnavailable(UNAVAILABLE_MESSAGE) from None
    # `leagueHistory` answers with a one-element list where the modern endpoint
    # answers with an object. Every caller here expects the object.
    if isinstance(payload, list):
        payload = payload[0] if payload else {}
    return payload if isinstance(payload, dict) else {}


def available_seasons(creds: EspnCredentials, league_id: str, years) -> list[int]:
    """Which of `years` this league actually existed for.

    A league created this year has none, and that is the state the Analysis tab
    has to explain rather than fail on. Detected by asking for teams: a season
    the league didn't exist for returns 404 or no teams.
    """
    found = []
    for year in years:
        try:
            payload = _get(creds, league_id, year, "mTeam")
        except EspnUnavailable:
            continue  # transient; treat as unknown rather than as "no season"
        if payload.get("teams"):
            found.append(int(year))
    return found


def _teams_frame(payload: dict, league_id: str, year: int) -> pd.DataFrame:
    rows = []
    for t in payload.get("teams") or []:
        record = ((t.get("record") or {}).get("overall") or {})
        rows.append({
            "team_id": t.get("id"),
            "team_name": t.get("name") or f"Team {t.get('id')}",
            "abbrev": t.get("abbrev"),
            "wins": record.get("wins"),
            "losses": record.get("losses"),
            "ties": record.get("ties"),
            "points_for": record.get("pointsFor"),
            "points_against": record.get("pointsAgainst"),
            "final_standing": t.get("rankCalculatedFinal") or t.get("playoffSeed"),
            "draft_projected_rank": t.get("draftDayProjectedRank"),
            "year": year,
            "league_id": str(league_id),
        })
    return pd.DataFrame(rows)


def _drafts_frame(payload: dict, league_id: str, year: int) -> pd.DataFrame:
    snapshot = parse_draft_detail(payload)
    rows = [{
        "player_id": p.player_id,
        "overallPickNumber": p.overall_pick,
        "roundId": p.round_id,
        "roundPickNumber": p.round_pick,
        "team_id": p.team_id,
        "autoDraftTypeId": 1 if p.autodrafted else 0,
        "lineupSlotId": None,
        "year": year,
        "league_id": str(league_id),
    } for p in snapshot.picks]
    return pd.DataFrame(rows)


def _players_frame(payload: dict, league_id: str, year: int) -> pd.DataFrame:
    """Season totals per player, actual and projected, under this league's scoring."""
    rows = []
    for entry in payload.get("players") or []:
        p = entry.get("player") or {}
        stats = p.get("stats") or []
        actual = next((s for s in stats if s.get("statSourceId") == ACTUAL
                       and s.get("statSplitTypeId") == SEASON_TOTAL), None)
        projected = next((s for s in stats if s.get("statSourceId") == PROJECTED
                          and s.get("statSplitTypeId") == SEASON_TOTAL), None)
        if actual is None and projected is None:
            continue
        detail = (actual or {}).get("stats") or {}
        row = {
            "player_id": p.get("id"),
            "player_name": p.get("fullName"),
            "year": year,
            "league_id": str(league_id),
            # eligibleSlots is the only reliable source of position - see
            # position_from_eligible_slots in notebooks/utils.py for the bug
            # that taught us not to use lineupSlot.
            "eligible_slots": json.dumps(p.get("eligibleSlots") or []),
            "points": (actual or {}).get("appliedTotal"),
            "avg_points": (actual or {}).get("appliedAverage"),
            "projected_points": (projected or {}).get("appliedTotal"),
            "projected_avg_points": (projected or {}).get("appliedAverage"),
        }
        for stat_id, col in STAT_IDS.items():
            row[col] = detail.get(stat_id)
        # Games played isn't reported directly; the average divides into the
        # total, which is what the notebook's figure has always meant.
        total, avg = row["points"], row["avg_points"]
        row["games_played"] = round(total / avg) if (total and avg) else None
        rows.append(row)

    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["position"] = df["eligible_slots"].map(position_from_eligible_slots).map(normalize_position)
    return df


def pull_season(creds: EspnCredentials, league_id: str, year: int) -> dict:
    """Everything one season contributes. Empty frames when it didn't exist."""
    teams = _teams_frame(_get(creds, league_id, year, "mTeam"), league_id, year)
    if teams.empty:
        return {"teams": teams, "drafts": pd.DataFrame(), "players_stats": pd.DataFrame()}
    drafts = _drafts_frame(_get(creds, league_id, year, "mDraftDetail"), league_id, year)
    players = _players_frame(
        _get(creds, league_id, year, "kona_player_info",
             headers={"x-fantasy-filter": json.dumps(PLAYER_FILTER)}),
        league_id, year,
    )
    return {"teams": teams, "drafts": drafts, "players_stats": players}


def store_season(engine, league_id: str, year: int, frames: dict) -> dict:
    """Replace this league-season's rows, leaving every other league alone.

    Delete-then-insert rather than append, so re-running a pull is idempotent
    instead of doubling the data - the mistake NB02's ADP insert documents.
    """
    written = {}
    with engine.begin() as conn:
        for table, df in frames.items():
            if df is None or df.empty:
                continue
            conn.execute(
                text(f"DELETE FROM {table} WHERE league_id = :lid AND year = :yr"),
                {"lid": str(league_id), "yr": year},
            )
    for table, df in frames.items():
        if df is None or df.empty:
            continue
        df.to_sql(table, engine, if_exists="append", index=False)
        written[table] = len(df)
    return written
