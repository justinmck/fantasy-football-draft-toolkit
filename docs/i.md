


![Alt text](espn_ff_logo.png)

Notes for now 

"1What makes a player a good draft pick, and how can I draft better to win my league in the future?”

Things to focus on:

player data:
player_id, fullName, position, teamId, total fantasy points, games played.

draft data:
player_id, round, pick, ADP


USE QUARTO FOR WEBSITE

schedule data:
Simplify it to teamId → Strength of Schedule score (aggregate opponents’ defensive ranks).

df_players columns needed:
    -   points
    -   avg_points
    -   projected_points
    -   projected_avg_points
    -   games_played
    -   actual_pointsScored
    -   player_name
    -   pro_team
    -   posRank
    -   player_id
    -   team_id
    -   team_name
    -   schedule
    -   position

adp_df columns needed:
    -   Rank
    -   Player
    -   player_id
    -   Team
    -   POS
        - Should seperate position from rank
    -   ESPN
    -   Sleeper
    -   AVG

df_draft columns needed:
    -   player_id
    -   overallPickNumber
    -   team_id
    -   roundPickNumber
    -   roundId
    -   autoDraftTypeId
    -   lineupSlotId

"


Core Questions to Answer
1. Which player had the most value last season?

    Should just do the difference between points scored and average draft position 

    Draft Value = (PPG – Replacement PPG at that position)/ overallPickNumber

    - Use player_data → fields like stats (season totals) and position.
        - Getting player data has been difficult
        - Only way to access it has been to go through the league team object and 
        then the roster which returns a list of players which you can then
        get the stats from.
        - The 0 
        
    - Calculate Fantasy Points per Game (PPG) so injuries don’t skew rankings.



2. Which players were most consistent vs. boom-or-bust?
    -Use weekly points (if you have them) → calculate:
    -Floor Rate (% of weeks above a baseline, e.g., 10+ points).
    -Boom Rate (% of weeks scoring +10 over their average).
    -Standard Deviation / Coefficient of Variation to measure volatility.

3. Which positions give the best value early vs. late rounds?
    -From draft_data (round, pick, ADP) → compare draft cost vs. production.
    -Calculate Value Over Replacement Player (VORP):
        -VORP=PPG Player − PPG Replacement Level at Position
​
 
4. Which players are best values relative to ADP (Average Draft Position)?
    -Find “sleepers” by comparing actual performance vs. where they were drafted.

5. Which players were overvalued in our league specifically and did that risk pay off?




Notes:
 - I want to make sure that games played is taken into account
 - Have to add losses and wins