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
        # Kept so a session can be reconstructed from scratch. ESPN sync
        # rebuilds the whole session by replaying every pick whenever the
        # remote state disagrees with what's been applied (see
        # src/espn_draft.py), and that needs the original configuration.
        self.roster_need = dict(roster_need)
        self.roster_state = {k: {"have": 0, "need": v} for k, v in roster_need.items()}
        # Players taken beyond a position's starting slots. Tracked separately
        # so `have` never exceeds `need` - otherwise the roster panel would
        # show "3/2" and, worse, open-slot math would go negative.
        self.depth = {}
        self.draft_log = []  # every pick made, in order, whichever team took it
        # Which principal owns this board. Set by `new_session`; empty only for
        # sessions constructed directly in tests.
        self.owner = ""

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

    def pick(self, player_id: int, position: str, mine=True, player_name: str | None = None,
             overall_pick: int | None = None):
        """Record a pick and, if it's mine, allocate it to a roster slot.

        NOT idempotent: `drafted_ids` is a set, but `draft_log` appends and
        `roster_state` increments unconditionally, so replaying the same pick
        double-counts a starting slot. Any caller that can see a pick more than
        once - the ESPN poller does, on every poll - must dedupe before calling.

        `overall_pick` is ESPN's own pick number, kept alongside our sequential
        `pick_number` because they diverge: ours counts what we've recorded,
        ESPN's is the true slot in the draft order. Manual drafts leave it None.
        """
        position = normalize_position(position)
        self.drafted_ids.add(player_id)
        filled = self._allocate(position) if mine else None
        self.draft_log.append({
            "pick_number": len(self.draft_log) + 1,
            "overall_pick": overall_pick,
            "player_id": player_id,
            "player_name": player_name,
            "position": position,
            "is_my_pick": mine,
            "filled_slot": filled,
            # False when the player couldn't be looked up, which means their
            # position is unknown and no roster slot was consumed. The UI has
            # to say so rather than showing a silently incomplete roster.
            "resolved": position is not None,
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

    @property
    def starting_slots(self) -> int:
        return sum(v["need"] for v in self.roster_state.values())

    def bench_slots(self) -> int | None:
        """Total bench spots: draft length minus the starting lineup.

        A 16-round draft with 9 starters is 7 bench picks - not a rounding
        detail but over a third of the draft, and previously invisible to the
        board, which stopped differentiating positions entirely once the
        starting lineup was full.
        """
        if not self.rounds:
            return None
        return max(self.rounds - self.starting_slots, 0)

    def bench_remaining(self) -> int | None:
        """Bench spots still to fill.

        Counted as "picks left after every open starting slot is accounted
        for", so an unfilled starter always claims a pick before the bench
        does - the board should never suggest a backup while a hole remains
        that it can't otherwise fill.
        """
        left = self.picks_remaining()
        if left is None:
            return None
        open_starters = sum(
            max(v["need"] - v["have"], 0) for v in self.roster_state.values()
        )
        return max(left - open_starters, 0)

    @property
    def bench_filled(self) -> int:
        return sum(self.depth.values())


SESSIONS = {}


def new_session(teams: int, roster_need: dict, rounds: int | None = None,
                owner: str = "") -> str:
    """Create a session owned by one principal.

    The id is a full uuid4, not the first 8 hex characters it used to be. At 32
    bits a session id is guessable, and a session id was a bearer capability:
    anyone holding one could read the board, inject picks - `pick` is not
    idempotent, so injected picks poison every later recommendation - poll ESPN
    on the owner's cookies, or disconnect them mid-draft.

    `owner` is a `Principal.user_id`, which is derived from the ESPN account
    rather than the device token, so re-signing in mid-draft keeps the board.
    """
    sid = uuid.uuid4().hex
    SESSIONS[sid] = DraftSession(teams, roster_need, rounds=rounds)
    SESSIONS[sid].owner = owner
    return sid


def get_session(sid: str) -> DraftSession:
    return SESSIONS[sid]
