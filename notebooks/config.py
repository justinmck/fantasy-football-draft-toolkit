POSITIONS = ["QB", "RB", "WR", "TE", "K", "DST"]

# CURRENT_SEASON = most recently completed season (source of "actuals" for
# analysis/validation). NEXT_SEASON = the upcoming draft this tool is for.
CURRENT_SEASON = 2025

NEXT_SEASON = 2026

TEAMS = 14

# Starting roster slots for the league. Used as the single definition of
# "replacement level" everywhere in the project (see src/scoring.py) instead
# of the hardcoded position-rank windows the codebase used to have scattered
# across multiple notebooks.
ROSTER_NEEDS = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DST": 1}
FLEX_ELIGIBLE = ("RB", "WR", "TE")

LINEUP_SLOT_MAP = {
    0: 'QB',
    1: 'TQB',
    2: 'RB',
    3: 'RB/WR',
    4: 'WR',
    5: 'WR/TE',
    6: 'TE',
    7: 'OP',
    8: 'DT',
    9: 'DE',
    10: 'LB',
    11: 'DL',
    12: 'CB',
    13: 'S',
    14: 'DB',
    15: 'DP',
    16: 'D/ST',
    17: 'K',
    18: 'P',
    19: 'HC',
    20: 'BE',
    21: 'IR',
    22: '',
    23: 'RB/WR/TE',
    24: 'ER',
    25: 'Rookie',
    'QB': 0,
    'RB': 2,
    'WR': 4,
    'TE': 6,
    'D/ST': 16,
    'K': 17,
    'FLEX': 23,
    'DT': 8,
    'DE': 9,
    'LB': 10,
    'DL': 11,
    'CB': 12,
    'S': 13,
    'DB': 14,
    'DP': 15,
    'HC': 19
}


