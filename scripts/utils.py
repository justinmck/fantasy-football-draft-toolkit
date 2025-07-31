from espn_api.football import League
from espn_api.requests import EspnFantasyRequests
import os
from dotenv import load_dotenv
import json
import pandas as pd 
import time

from espn_api.football import League


def save_to_data_raw(df, filename):
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
    save_path = os.path.join(project_root, "data", "raw", filename)

    # Ensure the folder exists
    os.makedirs(os.path.dirname(save_path), exist_ok=True)

    # Save the CSV (without adding the index unless needed)
    df.to_csv(save_path, index=False)
    print(f"File saved to: {save_path}")


def get_league_data(year):
    load_dotenv()

    league_id = os.getenv("LEAGUE_ID")
    espn_s2 = os.getenv("ESPN_S2")
    swid = os.getenv("SWID")

    if not all([league_id, espn_s2, swid]):
        raise EnvironmentError("Missing one or more required environment variables: LEAGUE_ID, ESPN_S2, SWID")

    return League(
        league_id=league_id,
        year=year,
        espn_s2=espn_s2,
        swid=swid,
    )

def process_players(players, team_name=None,team_id=None):
    """Takes a list of players (roster or free agents) and returns a DataFrame of their stats."""
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
        df_flat['current_team_id'] = team_id
        df_flat['current_team_name'] = team_name if team_id is not None else 'FA'
        df_flat['position'] = getattr(player, 'lineupSlot', None)
        df_flat['schedule'] = player.schedule
    
    
        all_stats.append(df_flat)
    return pd.concat(all_stats, ignore_index=True) if all_stats else pd.DataFrame()


def get_all_player_stats(league, num_fa=50):
    """Gets stats for all rostered players and free agents, combined in one DataFrame."""
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
        print(f"Pulled {len(all_free_agents)} free agents")
        all_dfs.append(free_df)

    # Combine all into one final DataFrame
    return pd.concat(all_dfs, ignore_index=True) if all_dfs else pd.DataFrame()