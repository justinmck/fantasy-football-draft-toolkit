import uuid

from src.scoring import FLEX_ELIGIBLE, normalize_position


class DraftSession:
    """Live state for one draft: who's gone, what the drafter still needs.

    `roster_state` is what `src/scoring.py`'s need weighting reads, so how
    picks are allocated to slots here directly drives the recommendations.
    """

    def __init__(self, teams: int, roster_need: dict, rounds: int | None = None):
        self.teams = teams
        self.rounds = rounds
        self.drafted_ids = set()
        self.roster_state = {k: {"have": 0, "need": v} for k, v in roster_need.items()}
        # Players taken beyond a position's starting slots. Tracked separately
        # so `have` never exceeds `need` - otherwise the roster panel would
        # show "3/2" and, worse, open-slot math would go negative.
        self.depth = {}
        self.draft_log = []  # every pick made, in order, whichever team took it

    def _allocate(self, position: str | None) -> str | None:
        """Assign a drafted player to the slot they actually fill.

        Order matters: a player's own starting slot first, then FLEX if they're
        eligible and it's open, then bench depth. Without the FLEX step nothing
        ever filled that slot (no player's position is literally "FLEX"), so
        the tool would keep recommending a third RB as though a starting slot
        were still open long after the roster was full.
        """
        slot = self.roster_state.get(position)
        if slot and slot["have"] < slot["need"]:
            slot["have"] += 1
            return position

        flex = self.roster_state.get("FLEX")
        if position in FLEX_ELIGIBLE and flex and flex["have"] < flex["need"]:
            flex["have"] += 1
            return "FLEX"

        if position:
            self.depth[position] = self.depth.get(position, 0) + 1
        return None

    def pick(self, player_id: int, position: str, mine=True, player_name: str | None = None):
        position = normalize_position(position)
        self.drafted_ids.add(player_id)
        filled = self._allocate(position) if mine else None
        self.draft_log.append({
            "pick_number": len(self.draft_log) + 1,
            "player_id": player_id,
            "player_name": player_name,
            "position": position,
            "is_my_pick": mine,
            "filled_slot": filled,
        })
        return filled

    @property
    def my_picks_made(self) -> int:
        return sum(1 for p in self.draft_log if p["is_my_pick"])

    def picks_remaining(self) -> int | None:
        """How many picks the drafter has left, if the draft length is known.

        Feeds `roster_urgency` in src/scoring.py: an open slot matters far more
        when there are two picks left than when there are twelve.
        """
        if not self.rounds:
            return None
        return max(self.rounds - self.my_picks_made, 0)


SESSIONS = {}


def new_session(teams: int, roster_need: dict, rounds: int | None = None) -> str:
    sid = uuid.uuid4().hex[:8]
    SESSIONS[sid] = DraftSession(teams, roster_need, rounds=rounds)
    return sid


def get_session(sid: str) -> DraftSession:
    return SESSIONS[sid]
