from espn_api.football import League
from espn_api.requests import EspnFantasyRequests
import ast
import json
import os, sys
from dotenv import load_dotenv

# Notebooks run with `notebooks/` as the working directory, so the repo root
# isn't importable by default and `from src.scoring import ...` below would
# fail on a cold kernel. Individual notebooks used to each append the root
# themselves, which meant importing utils before that line blew up; doing it
# here makes utils safe to import first.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from config import POSITIONS
import pandas as pd
import time
import re
from config import CURRENT_SEASON, TEAMS, ROSTER_NEEDS
from espn_api.requests.espn_requests import ESPNInvalidLeague
from espn_api.football import League
import unicodedata
from sqlalchemy import text
from src.scoring import add_vorp, compute_baselines, normalize_position


# Singular position slots, in the order we prefer them when a player is
# eligible at more than one. Combined slots ("RB/WR", "WR/TE", "RB/WR/TE",
# "OP") are deliberately NOT listed - matching is exact, so a WR whose slots
# are ["RB/WR", "WR", "WR/TE", "RB/WR/TE"] resolves to WR rather than RB.
_SINGULAR_POSITION_SLOTS = ("QB", "RB", "WR", "TE", "K")


def position_from_eligible_slots(slots):
    """Derive a player's real position from ESPN's `eligibleSlots` list.

    ESPN's player objects expose `lineupSlot` (where the player sat in a
    roster *this* week - "BE" for bench, "RB/WR/TE" for a flex start, or a
    raw slot id) and `eligibleSlots` (every roster slot the player is
    *allowed* to fill, which is a property of the player, not of any given
    week). Only the latter identifies position.

    This project originally recorded `lineupSlot` into the `position` column,
    which meant ~76% of every season's rows landed in `players_stats` labelled
    "0" or "BE" and were then silently dropped by every downstream
    `position.isin(POSITIONS)` filter. Deriving from `eligibleSlots` instead
    recovers 99.97% of rows and agrees with the existing labels on 100% of
    the rows that were already correct.

    Accepts a list, a JSON string (as stored in the DB), or a Python-repr
    string (as stored in the raw CSVs). Returns None when no fantasy position
    is present - e.g. punters, whose only eligible slot is "P".
    """
    if slots is None:
        return None
    if isinstance(slots, str):
        text_value = slots.strip()
        if not text_value:
            return None
        try:
            slots = json.loads(text_value)
        except (ValueError, TypeError):
            try:
                slots = ast.literal_eval(text_value)
            except (ValueError, SyntaxError):
                return None
    if not isinstance(slots, (list, tuple, set)):
        return None

    available = {str(s).strip() for s in slots}
    for position in _SINGULAR_POSITION_SLOTS:
        if position in available:
            return position
    # Defense is the one position ESPN spells differently from our POSITIONS
    # constant, so normalize it here rather than leaving it to callers.
    if "D/ST" in available or "DST" in available:
        return "DST"
    return None


def load_season_stats(engine, year):
    """One row per drafted player for `year`: actual season stats, draft
    slot, team, and ADP. No VORP here - that's computed afterward via
    src/scoring.py so retrospective and live-draft VORP share one
    replacement-baseline definition instead of each having their own.

    Shared here (rather than defined inline in a single notebook) because
    both NB03 (retrospective draft-value analysis) and NB05 (projection
    accuracy) need the same per-player/per-season row shape and shouldn't
    duplicate this SQL.
    """
    query = """
    SELECT
        d.player_id,
        p.player_name,
        s.posRank,
        s.points,
        s.games_played,
        t.team_name,
        a.position,
        s.avg_points,
        s.projected_points,
        (s.points - s.projected_points) AS boom_bust,
        d.lineupSlotId,
        d.roundPickNumber,
        d.overallPickNumber,
        s.pro_team,
        a.avg,
        t.final_standing,
        t.draft_projected_rank,
        (d.overallPickNumber - a.avg) AS draft_delta,
        d.roundId
    FROM drafts d
    JOIN players p ON d.player_id = p.player_id
    JOIN players_stats s
         ON d.player_id = s.player_id
        AND d.year = s.year
    JOIN teams t
         ON d.team_id = t.team_id
        AND d.year = t.year
    JOIN average_draft_position a
         ON d.player_id = a.player_id
        AND d.year = a.year
    WHERE s.points IS NOT NULL
      AND d.year = :year
    """
    df = pd.read_sql(text(query), engine, params={"year": year})
    df["position"] = df["position"].map(normalize_position)
    return df


def load_projection_actuals(engine, year):
    """One row per player with a recorded preseason projection and actual
    season outcome for `year` - every player ESPN tracked stats for, not
    just players drafted in this specific league.

    Unlike load_season_stats(), this doesn't join through
    average_draft_position (only on record from 2022 onward), so it covers
    the full 2020-2025 history and is the dataset used for projection
    accuracy analysis (NB05), where the goal is "how good were the
    projections overall" rather than "how did my league's draft go".

    `projected_points = 0` is excluded as a missing-data sentinel, not a
    legitimate zero projection: year 2023 has an extraction gap where nearly
    every player's projected_points was recorded as 0.0 (verified against
    the raw source - e.g. Patrick Mahomes 2023 shows points=267.0 but
    projected_points=0.0), which otherwise swamps that year's error metrics
    with the entire actual-points total as "error". Other years have only a
    handful of isolated zero rows, which this same filter also correctly
    drops.
    """
    query = """
    SELECT player_id, player_name, position, pro_team,
           projected_points, points AS actual_points
    FROM players_stats
    WHERE year = :year
      AND projected_points IS NOT NULL
      AND projected_points != 0
      AND points IS NOT NULL
    """
    df = pd.read_sql(text(query), engine, params={"year": year})
    df["position"] = df["position"].map(normalize_position)
    df["year"] = year
    return df


def add_projection_and_actual_vorp(df, teams=TEAMS, roster_needs=None):
    """Add `proj_vorp`/`actual_vorp` to a single-season projection-vs-actual
    dataframe (one row per player, `projected_points` and `actual_points`
    columns) - i.e. answer "how good was the *VORP* number", not just "how
    close were the raw points".

    Each column gets its own replacement-level baseline, computed from that
    column's own distribution for this season, using the same
    compute_baselines()/add_vorp() definition src/scoring.py uses everywhere
    else. `proj_vorp` is what the live draft tool would have shown that
    player on draft day; `actual_vorp` is what they were actually worth once
    the season played out - the gap between the two is the real error that
    matters for drafting, since a raw-points miss on a player who still
    finishes above/below the same replacement line barely changes the
    draft-day call.
    """
    roster_needs = roster_needs or ROSTER_NEEDS
    proj_baselines = compute_baselines(df, teams=teams, roster_needs=roster_needs, value_col="projected_points")
    out = add_vorp(df, proj_baselines, value_col="projected_points", out_col="proj_vorp")
    actual_baselines = compute_baselines(out, teams=teams, roster_needs=roster_needs, value_col="actual_points")
    out = add_vorp(out, actual_baselines, value_col="actual_points", out_col="actual_vorp")
    return out.drop(columns="baseline")


def save_to_data_raw(df, filename, year):
    """
    Saves a DataFrame to the project's data/raw folder, 
    even if the code is run from the notebooks directory.

    Parameters:
    df (pd.DataFrame): The DataFrame to save.
    filename (str): The CSV filename (e.g., "player_stats.csv").
    """
    # Step up one directory (from notebooks to project root if needed)
    project_root = os.path.abspath(os.path.join(os.getcwd(), ".."))

    # Build the full save path
    save_path = f"{project_root}/data/raw/{year}/league_stats/{filename}_{year}.csv"

    # Ensure the folder exists
    os.makedirs(os.path.dirname(save_path), exist_ok=True)

    # Save the CSV (without adding the index unless needed)
    df.to_csv(save_path, index=False)
    print(f"File saved to: {save_path}")

def read_csvs(path="league_stats", data="player_data", years=None):
    if years is None:
        years = [CURRENT_SEASON]

    root = "../data/raw"
    dfs = []

    for year in years:
        if path == "adp":
            file_path = f"{root}/{year}/{path}/FantasyPros_{year}_Overall_ADP_Rankings.csv"
            if os.path.exists(file_path):
                df = clean_adp(file_path)
                df["year"] = year
                dfs.append(df)
            else:
                print(f"⚠️ File not found for year {year}: {file_path}")
        elif path == "projections":
            for pos in POSITIONS:
                file_path = f"{root}/{year}/{path}/FantasyPros_Fantasy_Football_Projections_{pos}.csv"
                if os.path.exists(file_path):
                    df = pd.read_csv(file_path)
                    df["position"] = pos
                    df["year"] = year
                    dfs.append(df)
                else:
                    print(f"⚠️ File not found: {file_path}")

        else:
            file_path = f"{root}/{year}/{path}/{data}_{year}.csv"
            if os.path.exists(file_path):
                df = pd.read_csv(file_path)
                df["year"] = year
                dfs.append(df)
            else:
                print(f"⚠️ File not found for year {year}: {file_path}")

    if dfs:
        return pd.concat(dfs, ignore_index=True)
    else:
        print("❌ No files were loaded.")
        return pd.DataFrame()

def get_league_data(year):
    """
    Initializes and returns an ESPN fantasy football League object for the given year.

    This function loads environment variables from a `.env` file to authenticate the user:
    - LEAGUE_ID: the unique identifier of the league
    - ESPN_S2: session token (from cookies)
    - SWID: unique identifier for the user

    Raises:
        EnvironmentError: If any of the required environment variables are missing.

    Args:
        year (int): The year of the fantasy football season.

    Returns:
        League: An instance of the League object from the espn-api package.
    """
    load_dotenv()

    league_id = os.getenv("LEAGUE_ID")
    espn_s2 = os.getenv("ESPN_S2")
    swid = os.getenv("SWID")

    if not all([league_id, espn_s2, swid]):
        raise EnvironmentError("Missing one or more required environment variables: LEAGUE_ID, ESPN_S2, SWID")

    try:
        return League(
            league_id=league_id,
            year=year,
            espn_s2=espn_s2,
            swid=swid,
        )
    except ESPNInvalidLeague as e:
        print(f"Invalid year {year}.")
        return None

def process_players(players, team_name=None,team_id=None):
    """
    Processes a list of fantasy football player objects and returns a flat DataFrame of their stats.

    This function extracts and normalizes each player's:
    - Core stats
    - Actual and projected stat breakdowns
    - Metadata (name, team, position, etc.)

    If breakdown data is nested, it's flattened and prefixed for clarity.
    Missing or null values are handled gracefully.

    Args:
        players (list): List of Player objects from a roster or free agents pool.
        team_name (str, optional): Name of the fantasy team (used for context).
        team_id (int, optional): ID of the fantasy team (used to link to owner/team).

    Returns:
        pd.DataFrame: A combined DataFrame of all player stats and metadata.
    """
    all_stats = []
    for player in players:
        if not player.stats:
                continue
        ## Gets stats
        stats = player.stats[0]
        stats_df = pd.DataFrame([stats])

        breakdown_data = stats.get('breakdown', {}) or {}
        proj_breakdown_data = stats.get('projected_breakdown', {}) or {}

        # Normalize nested breakdown
        breakdown_df = pd.json_normalize(breakdown_data)
        proj_breakdown_df = pd.json_normalize(proj_breakdown_data)
    
        # Combine everything into one flat DataFrame
        df_flat = pd.concat([
        stats_df.drop(columns=['breakdown', 'projected_breakdown'], errors='ignore').reset_index(drop=True),
        breakdown_df.add_prefix('actual_'),           # prefix so we know which is actual
        proj_breakdown_df.add_prefix('proj_')         # prefix for projections
        ], axis=1)
    
        # Safely fetch player attributes
        df_flat['player_name'] = getattr(player, 'name', 'Unknown')
        df_flat['pro_team'] = getattr(player, 'proTeam', 'Unknown')
        df_flat['acquisition_type'] = getattr(player, 'acquisitionType', 'FA') or 'FA'
        df_flat['posRank'] = getattr(player, 'posRank', None)
        df_flat['player_id'] = getattr(player, 'playerId', None)
        df_flat['team_id'] = team_id
        df_flat['team_name'] = team_name if team_id is not None else 'FA'
        # `position` must come from eligibleSlots, not lineupSlot: lineupSlot
        # is where the player sat that week ("BE", "RB/WR/TE", or a raw slot
        # id), which is a roster decision, not the player's position. The
        # lineupSlot value is still preserved separately below.
        eligible_slots = getattr(player, 'eligibleSlots', []) or []
        df_flat['position'] = position_from_eligible_slots(eligible_slots)
        df_flat['schedule'] = [getattr(player, 'schedule', None)]
        df_flat['eligible_slots'] = [eligible_slots]
        df_flat['lineup_slot'] = player.lineupSlot
    
        all_stats.append(df_flat)
    return pd.concat(all_stats, ignore_index=True) if all_stats else pd.DataFrame()


def get_all_player_stats(league, num_fa=50):
    """
    Collects and combines stats for all rostered players and free agents in a fantasy league.

    This function:
    - Iterates over each team in the league and extracts player stats via `process_players`
    - Pulls a specified number of free agents and processes them as well
    - Returns a unified DataFrame containing all player stats, enriched with team metadata

    Args:
        league (League): A `fantasy-football-league` object instance.
        num_fa (int, optional): Number of free agents to retrieve and include. Defaults to 50.

    Returns:
        pd.DataFrame: A DataFrame of all rostered and free agent players with their stats and metadata.
    """
    all_dfs = []
    # Rostered players
    for team in league.teams:
        team_df = process_players(team.roster, team_name=team.team_name, team_id=team.team_id)
        all_dfs.append(team_df)

    # Free agents
    all_free_agents = league.free_agents(size=num_fa)

    # Process all free agents
    if all_free_agents:
        free_df = process_players(all_free_agents)
        print(f"Pulled {len(all_free_agents)} free agents from {league.year}")
        all_dfs.append(free_df)

    # Combine all into one final DataFrame
    return pd.concat(all_dfs, ignore_index=True) if all_dfs else pd.DataFrame()

def process_projections(players):
    """
    Processes a list of fantasy football Player objects into a projections DataFrame.
    Designed for future seasons where actual stats are not yet available.
    """

    all_stats = []
    for player in players:
        if not player.stats:
                continue
        ## Gets stats
        stats = player.stats[0]
        stats_df = pd.DataFrame([stats])
        proj_breakdown_data = stats.get('projected_breakdown', {}) or {}

        # Normalize nested breakdown
        proj_breakdown_df = pd.json_normalize(proj_breakdown_data)
    
        # Combine everything into one flat DataFrame
        df_flat = pd.concat([
        stats_df.drop(columns=['breakdown', 'projected_breakdown'], errors='ignore').reset_index(drop=True),
        proj_breakdown_df.add_prefix('proj_')         # prefix for projections
        ], axis=1)


        proj_points = getattr(player, 'projected_total_points', None)
        proj_breakdown_data = getattr(player, 'projected_breakdown', {}) or {}

        proj_breakdown_df = pd.json_normalize(proj_breakdown_data)

        df_flat = proj_breakdown_df.add_prefix('proj_')
        df_flat['player_name'] = getattr(player, 'name', 'Unknown')
        df_flat['pro_team'] = getattr(player, 'proTeam', 'Unknown')
        df_flat['player_id'] = getattr(player, 'playerId', None)
        df_flat['projected_points'] = proj_points
        df_flat['eligible_slots'] = [getattr(player, 'eligibleSlots', [])]

        all_stats.append(df_flat)

    return pd.concat(all_stats, ignore_index=True) if all_stats else pd.DataFrame()


def team_name_to_dst(df):
    team_to_dst = {
    "San Francisco 49ers": "49ers D/ST",
    "Chicago Bears": "Bears D/ST",
    "Cincinnati Bengals": "Bengals D/ST",
    "Buffalo Bills": "Bills D/ST",
    "Denver Broncos": "Broncos D/ST",
    "Cleveland Browns": "Browns D/ST",
    "Tampa Bay Buccaneers": "Buccaneers D/ST",
    "Arizona Cardinals": "Cardinals D/ST",
    "Los Angeles Chargers": "Chargers D/ST",
    "Kansas City Chiefs": "Chiefs D/ST",
    "Indianapolis Colts": "Colts D/ST",
    "Washington Commanders": "Commanders D/ST",
    "Dallas Cowboys": "Cowboys D/ST",
    "Miami Dolphins": "Dolphins D/ST",
    "Philadelphia Eagles": "Eagles D/ST",
    "Atlanta Falcons": "Falcons D/ST",
    "New York Giants": "Giants D/ST",
    "Jacksonville Jaguars": "Jaguars D/ST",
    "New York Jets": "Jets D/ST",
    "Detroit Lions": "Lions D/ST",
    "Green Bay Packers": "Packers D/ST",
    "Carolina Panthers": "Panthers D/ST",
    "New England Patriots": "Patriots D/ST",
    "Las Vegas Raiders": "Raiders D/ST",
    "Los Angeles Rams": "Rams D/ST",
    "Baltimore Ravens": "Ravens D/ST",
    "New Orleans Saints": "Saints D/ST",
    "Seattle Seahawks": "Seahawks D/ST",
    "Pittsburgh Steelers": "Steelers D/ST",
    "Houston Texans": "Texans D/ST",
    "Tennessee Titans": "Titans D/ST",
    "Minnesota Vikings": "Vikings D/ST",
    }
    if 'player_name' not in df.columns:
        print("No column name 'player_name'. Make sure the dataframe has a column named 'Player'")   
    
    
    df['player_name'] = df['player_name'].replace(team_to_dst)
    return df


def clean_name(name):
    if pd.isna(name):
        return ""
    
    # Normalize Unicode characters (e.g., é → e)
    name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("utf-8")

    # Remove suffixes like Jr., Sr., etc.
    name = re.sub(r'\b(jr\.?|sr\.?|ii|iii|iv|v)\b', '', name, flags=re.IGNORECASE)

    # Replace dashes and apostrophes with space or nothing
    name = name.replace("-", " ").replace("'", "")

    # Remove all punctuation except spaces
    name = re.sub(r'[^\w\s]', '', name)

    # Strip extra whitespace and lowercase
    name = ' '.join(name.strip().lower().split())

    # (Optional) Remove middle name if there are 3 parts
    parts = name.split()
    if len(parts) == 3:
        name = f"{parts[0]} {parts[2]}"

    return name

# FantasyPros changed their export format for 2026: instead of separate
# Player / Team / Bye columns, the three are merged into one
# "Player (Bye)" column, e.g.
#
#   "Jahmyr Gibbs   DET (6)"        -> Jahmyr Gibbs,     DET, bye 6
#   "Houston Texans DST   (8)"      -> Houston Texans,   DST, bye 8
#   "Stefon Diggs"                  -> Stefon Diggs,     no team (free agent)
#
# The team token is the last all-caps run before the parenthesised bye week.
# Defenses carry the literal team code "DST", which matches how earlier years
# recorded them (Player="Denver Broncos", Team="DST") - so splitting this way
# keeps team_name_to_dst() working unchanged across every season.
_COMBINED_PLAYER_RE = re.compile(r"^(?P<name>.+?)\s+(?P<team>[A-Z/]{2,4})\s*\((?P<bye>\d+)\)\s*$")


def _split_combined_player_column(df):
    """Normalise FantasyPros' 2026+ "Player (Bye)" column into the
    player_name / team_name / bye columns every earlier season already has."""
    combined = df["Player (Bye)"].astype(str).str.strip()
    parts = combined.str.extract(_COMBINED_PLAYER_RE)
    # Rows with no team/bye (free agents) still have a usable name - fall back
    # to the raw string rather than dropping the player entirely.
    df["player_name"] = parts["name"].fillna(combined)
    df["team_name"] = parts["team"]
    df["bye"] = pd.to_numeric(parts["bye"], errors="coerce")
    return df.drop(columns="Player (Bye)")


def clean_adp(csv):
    try:
        df = pd.read_csv(csv, quotechar='"', escapechar='\\', on_bad_lines='skip')
    except FileNotFoundError:
        print(f"File not found. {csv}")
        df = pd.DataFrame()
    except pd.errors.EmptyDataError:
        print("CSV is empty.")
        df = pd.DataFrame()
    except pd.errors.ParserError:
        print("CSV is malformed.")
        df = pd.DataFrame()

    if df.empty:
        return df

    if "Player (Bye)" in df.columns:
        df = _split_combined_player_column(df)

    ## Rename the columns to match conventions
    df = df.rename(columns={
    "Player": "player_name",
    "Team": "team_name",
    "AVG": "avg",
    "Rank": "rank",
    "Bye": "bye"
    })

    # Per-source pick columns use an em dash for "this source didn't rank them".
    # Left as text they'd poison the column dtype and land in the database as
    # strings, so coerce every numeric column explicitly.
    for col in ("rank", "avg", "bye", "ESPN", "Sleeper", "NFL", "RTSports",
                "FFC", "CBS", "Fantrax", "Real-Time"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    # Seperate position and rank
    df["position"] = df["POS"].str.extract(r'([A-Za-z]+)')
    df["pos_rank"] = df["POS"].str.extract(r'(\d+)').astype(float)
    # Map Defences
    df = team_name_to_dst(df)
    return df




    

def generate_schema_from_df(df, table_name, primary_keys=None, foreign_keys=None):
    sql_types = {
        "int64": "INTEGER",
        "float64": "DECIMAL(10, 4)",
        "object": "TEXT",
        "bool": "BOOLEAN",
    }

    cols = []
    for col in df.columns:
        dtype = str(df[col].dtype)
        sql_type = sql_types.get(dtype, "TEXT")
        cols.append(f'"{col}" {sql_type}')

    if primary_keys:
        cols.append(f"PRIMARY KEY ({', '.join(primary_keys)})")

    if foreign_keys:
        for fk_col, ref in foreign_keys.items():
            cols.append(f"FOREIGN KEY ({fk_col}) REFERENCES {ref}")

    return f"CREATE TABLE {table_name} (\n    " + ",\n    ".join(cols) + "\n);"


