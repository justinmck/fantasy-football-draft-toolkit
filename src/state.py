import uuid

from src.scoring import normalize_position


class DraftSession:
    def __init__(self, teams: int, roster_need: dict):
        self.teams = teams
        self.drafted_ids = set()
        self.roster_state = {k: {"have": 0, "need": v} for k, v in roster_need.items()}
        self.draft_log = []  # every pick made, in order, whichever team took it

    def pick(self, player_id: int, position: str, mine=True, player_name: str | None = None):
        position = normalize_position(position)
        self.drafted_ids.add(player_id)
        if mine and position in self.roster_state:
            self.roster_state[position]["have"] += 1
        self.draft_log.append({
            "pick_number": len(self.draft_log) + 1,
            "player_id": player_id,
            "player_name": player_name,
            "position": position,
            "is_my_pick": mine,
        })

SESSIONS = {}
def new_session(teams: int, roster_need: dict) -> str:
    sid = uuid.uuid4().hex[:8]
    SESSIONS[sid] = DraftSession(teams, roster_need)
    return sid
def get_session(sid: str) -> DraftSession:
    return SESSIONS[sid]
