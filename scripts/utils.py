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



def get_rostered_player_stats(league):
    all_stats = []
    for team in league.teams:
        for player in team.roster:
            if not player.stats:
                continue
            stats = player.stats[0]
            stats_df = pd.DataFrame([stats])
            breakdown_df = pd.json_normalize(stats_df['breakdown'])
            proj_breakdown_df = pd.json_normalize(stats_df['projected_breakdown'])
            df_flat = pd.concat([
            stats_df.drop(columns=['breakdown', 'projected_breakdown']).reset_index(drop=True),
            breakdown_df.add_prefix('actual_'),           # prefix so we know which is actual
            proj_breakdown_df.add_prefix('proj_')         # prefix for projections
            ], axis=1)
            df_flat['player_name'] = player.name
            df_flat['pro_team'] = player.proTeam
            df_flat['acquisition_type'] = player.acquisitionType
            df_flat['posRank'] = player.posRank
            df_flat['player_id'] = player.playerId
            df_flat['team_id'] = player.onTeamId
            df_flat['team_name'] = team.team_name
            all_stats.append(df_flat)
    final_df = pd.concat(all_stats, ignore_index=True) if all_stats else pd.DataFrame()
    final_df = final_df.set_index(['player_name',
                                   'player_id',
                                   'team_name',
                                   'team_id',
                                   'acquisition_type'])
    return final_df
