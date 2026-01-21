Website:

NB04:
- Create a draftboard for next season 
- ML to see who's available and best for my team 
"Given who's available, who should I draft right now to maximize my team’s season-long value?"
- Best models:
    - Regression:
        - XGBoost
        - Random forest Regressor
        - CatBoost
For right now focus on building the site using yearly data. Expand to weekly data later on. 

TARGET: y=fantasy_points
Eventually: y=fantasy_points_per_game

✅ Good Features to Use:
Only include features you would know before the season starts, such as:

Projected points (total and per game)

Projected rushing/receiving/passing stats

Player position

Team info (e.g., current team name, bye week, team rank)

Historical data from previous years

Draft ADP (ESPN/Sleeper)

Roster role (starter/bench, depth chart rank if available)

Revisions:
Should VORP be calculated using a lower position number?
- Vorp currently contains players that are on the bench, but doesn't indicate if there are players on the waiver wire who are better. 

- NB02: 
    - Creating a function to analyze the adp from 2022 to 2025. 
    - Need to get all that data to do more analysis
    - 


Future Analysis



What columns needed for testing
    - Not schedule
    - projected points
    - last years VORP
    - last years ranking