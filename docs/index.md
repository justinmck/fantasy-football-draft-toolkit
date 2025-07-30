Notes for now 

Things to focus on:

player data:
player_id, fullName, position, teamId, total fantasy points, games played.

draft data:
player_id, round, pick, ADP

schedule data:
Simplify it to teamId → Strength of Schedule score (aggregate opponents’ defensive ranks).



Core Questions to Answer
1. Which players scored the most fantasy points last season?

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

5. (Optional): How much should schedule strength matter?

You don’t need every detail from proTeams; just figure out if a player’s team faces a tough or easy schedule by opponent defense ranking.

A simple “Strength of Schedule” score per team is enough — you don’t need every matchup detail unless you’re building week-by-week projections.



Notes:
 - I want to make sure that games played is taken into account
 - Have to add losses and wins