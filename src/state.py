import uuid
from collections import defaultdict

class DraftSession:
    def __init__(self, teams: int, roster_need: dict):
        self.teams = teams
        self.drafted_ids = set()
        self.roster_state = {k: {"have":0, "need":v} for k, v in roster_need.items()}

    def pick(self, player_id: int, position: str, mine=True):
        self.drafted_ids.add(player_id)
        if mine and position in self.roster_state:
            self.roster_state[position]["have"] += 1

SESSIONS = {}
def new_session(teams: int, roster_need: dict) -> str:
    sid = uuid.uuid4().hex[:8]
    SESSIONS[sid] = DraftSession(teams, roster_need)
    return sid
def get_session(sid: str) -> DraftSession:
    return SESSIONS[sid]
