"""Live sync against an in-progress ESPN draft.

Polls one endpoint, detects picks as they happen, works out which are yours,
and applies them to a `DraftSession` so the board re-ranks itself without
anyone clicking anything.

    GET .../seasons/<year>/segments/0/leagues/<id>?view=mDraftDetail

Three things about ESPN's payload drive the whole design:

1. **The pre-draft response is not empty.** It already contains every pick slot
   - 224 for a 14-team, 16-round draft - each with `playerId: -1` and its
   `teamId` already assigned. So the full snake order is knowable before the
   draft starts, and the "has this pick happened" test is `playerId != -1`.
   Anything testing `if picks:` or `len(picks)` concludes the draft is finished
   before it has begun.

2. **ESPN returns the entire pick list every time, not a delta.** Session state
   is therefore a pure function of the latest payload: a missed poll costs
   nothing, and any disagreement can be resolved by rebuilding from scratch.
   That is what makes a simple pull-through poll correct, and why there is no
   background worker here.

3. **It honours `If-None-Match`.** Nothing changes between picks, so the large
   majority of polls return 304 with an empty body.

Why this does not use the `espn_api` package
--------------------------------------------
`League.draft` / `refresh_draft()` cannot see a live draft at all:
`_fetch_draft` early-returns unless `draftDetail.drafted` is true, and that is
a *completion* flag - so the list stays empty for the entire duration of the
draft and only fills in afterwards. (It also appends without clearing, so
refreshing duplicates every pick, and `refresh_draft(refresh__teams=True)`
raises `NameError` on an undefined local.)

**This module must never import `espn_api`, directly or transitively** - which
also rules out `notebooks.utils`, since that imports it at module scope. The
package's `ESPNAccessDenied` interpolates `espn_s2` and `swid` into its message
string, so an exception escaping into a log line or an API response would leak
the session cookie. Not importing it is a structural guarantee rather than a
promise to be careful; `tests/test_espn_draft.py` pins it.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field

import requests
from dotenv import load_dotenv
from sqlalchemy import text

from src.state import DraftSession

BASE = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/ffl"
# The "fan" API is how a SWID is turned into the list of leagues it belongs to,
# so nobody has to find and paste league ids by hand.
FAN_BASE = "https://fan.api.espn.com/apis/v2/fans"
FAN_PARAMS = {
    "featureFlags": "challengeEntries",
    "showAirings": "buy,live,replay",
    "source": "ESPN.com+-+FAN+Landing+Page",
    "lang": "en",
    "section": "espn",
    "region": "us",
}
# ESPN's game ids: 1 is football. The fan payload mixes in every fantasy sport
# the user plays, so this filter is doing real work.
FOOTBALL_GAME_ID = 1

# ESPN lineup slot id -> the key this project's ROSTER_NEEDS uses. Slot 23 is
# "RB/WR/TE", which is what everyone calls FLEX. Bench (20) and IR (21) are
# deliberately absent: they aren't starting slots, and `roster_need` drives the
# replacement-level baseline, which is defined by who *starts*.
STARTING_SLOTS = {0: "QB", 2: "RB", 4: "WR", 6: "TE", 16: "DST", 17: "K", 23: "FLEX"}
BENCH_SLOT, IR_SLOT = 20, 21

# ESPN's placeholder in an unfilled pick slot. The single most important
# constant here: see point 1 in the module docstring.
UNFILLED = -1

# Never formatted, never interpolated. A 401 from ESPN means the stored cookies
# are stale, and the only safe thing to say about it is that.
AUTH_MESSAGE = "ESPN rejected the stored credentials. Refresh SWID and ESPN_S2 in .env."
UNAVAILABLE_MESSAGE = "Could not reach ESPN."


class EspnAuthError(RuntimeError):
    """Credentials rejected. Message is a constant - never echo the response."""


class EspnUnavailable(RuntimeError):
    """Network failure or an unusable response."""


@dataclass(frozen=True)
class EspnCredentials:
    # Ordered so `league_id` stays first for existing positional callers. It is
    # now only a *default* - the app supports several leagues and passes the id
    # per connection - so it may legitimately be empty.
    league_id: str
    swid: str
    espn_s2: str


@dataclass(frozen=True)
class LeagueRef:
    """One of the user's leagues, as the picker sees it."""
    league_id: str
    name: str
    season: int


@dataclass(frozen=True)
class LeagueSettings:
    """What a league is: its shape, and whether its draft has a date yet."""
    name: str | None
    size: int | None
    rounds: int | None
    roster_need: dict
    bench_slots: int | None
    # Epoch milliseconds, or None when ESPN hasn't been given a date. That
    # absence is the *only* signal for "not scheduled yet" - see parse_settings.
    draft_at: int | None
    draft_type: str | None

    @property
    def scheduled(self) -> bool:
        return self.draft_at is not None


@dataclass(frozen=True)
class EspnPick:
    overall_pick: int
    round_id: int
    round_pick: int
    team_id: int
    player_id: int
    autodrafted: bool = False
    keeper: bool = False


@dataclass(frozen=True)
class DraftSnapshot:
    """One reading of the draft: the order, and whichever picks have happened."""
    order: tuple[int, ...]          # team id per overall pick, index 0 == pick 1
    picks: tuple[EspnPick, ...]     # completed only, sorted by overall_pick
    total_slots: int
    teams: int
    rounds: int

    @property
    def complete(self) -> bool:
        return len(self.picks) >= self.total_slots > 0

    @property
    def current_pick(self) -> int | None:
        """The lowest slot still unfilled.

        Not `len(picks) + 1`: keepers and commissioner edits can fill slots out
        of order, and the count form then mis-reports the clock for the rest of
        the draft. None once every slot is filled.
        """
        filled = {p.overall_pick for p in self.picks}
        for n in range(1, self.total_slots + 1):
            if n not in filled:
                return n
        return None


# ---------------------------------------------------------------------------
# credentials
# ---------------------------------------------------------------------------

def load_credentials() -> EspnCredentials:
    """Read ESPN credentials from the environment.

    Called lazily, never at import: `src/api.py` is imported by the test suite
    with no ESPN variables set, and loading here at module scope would fail
    collection for every test in the project.
    """
    load_dotenv()
    swid = os.getenv("SWID")
    espn_s2 = os.getenv("ESPN_S2")
    # LEAGUE_ID is no longer required. SWID and ESPN_S2 are the actual
    # credentials, and the leagues they can reach are discovered from them - so
    # the id in .env is just a default for the notebooks and a fallback when no
    # league has been picked.
    if not all([swid, espn_s2]):
        raise EspnAuthError(
            "Missing SWID or ESPN_S2 in .env - see the README for how to find them."
        )
    return EspnCredentials(
        league_id=str(os.getenv("LEAGUE_ID") or ""),
        swid=str(swid),
        espn_s2=str(espn_s2),
    )


# ---------------------------------------------------------------------------
# parsing - pure, no I/O
# ---------------------------------------------------------------------------

def parse_fan_leagues(payload: dict, year: int) -> list[LeagueRef]:
    """Every football league this SWID belongs to in `year`.

    The fan payload lists all of a user's fantasy entries across every sport
    and season, so both filters matter: `gameId == 1` for football, and the
    season, or you get last year's leagues mixed in with this year's.

    Written defensively because this is a public-ish endpoint whose shape we
    don't control - a malformed entry is skipped rather than raising, since one
    odd league shouldn't stop the picker rendering the others.
    """
    out, seen = [], set()
    for pref in payload.get("preferences") or []:
        entry = ((pref or {}).get("metaData") or {}).get("entry") or {}
        if entry.get("gameId") != FOOTBALL_GAME_ID:
            continue
        if year is not None and entry.get("seasonId") != year:
            continue
        groups = entry.get("groups") or []
        if not groups:
            continue  # an entry with no league attached is not selectable
        group = groups[0] or {}
        league_id = group.get("groupId")
        if league_id is None or league_id in seen:
            continue
        seen.add(league_id)
        out.append(LeagueRef(
            league_id=str(league_id),
            name=(group.get("groupName") or f"League {league_id}").strip(),
            season=int(entry.get("seasonId") or year),
        ))
    return out


def roster_need_from_slots(lineup_slot_counts: dict) -> dict:
    """ESPN's lineup slot counts -> this project's ROSTER_NEEDS shape.

    Read from the league rather than assumed. All three of the user's leagues
    happen to share the standard shape today, but a league that starts two
    quarterbacks would silently get the wrong replacement baseline - and VORP is
    defined entirely by who starts.
    """
    need = {}
    for raw_slot, count in (lineup_slot_counts or {}).items():
        try:
            slot, count = int(raw_slot), int(count)
        except (TypeError, ValueError):
            continue
        name = STARTING_SLOTS.get(slot)
        if name and count > 0:
            need[name] = need.get(name, 0) + count
    return need


def parse_settings(payload: dict) -> LeagueSettings:
    """League shape and draft schedule from a `view=mSettings` response.

    **`draftSettings.date` being absent is the whole "not scheduled" signal.**
    ESPN doesn't send a null or a sentinel - for a league with no date set the
    key simply isn't in the object (verified against a scheduled and an
    unscheduled league). So `.get("date")` returning None is the discriminator,
    and nothing else should be invented for it.
    """
    settings = payload.get("settings") or {}
    draft = settings.get("draftSettings") or {}
    roster = settings.get("rosterSettings") or {}
    counts = roster.get("lineupSlotCounts") or {}

    need = roster_need_from_slots(counts)
    bench = None
    try:
        bench = int(counts.get(str(BENCH_SLOT)) or counts.get(BENCH_SLOT) or 0) or None
    except (TypeError, ValueError):
        bench = None

    size = settings.get("size")
    starters = sum(need.values())
    rounds = (starters + bench) if (bench is not None and starters) else None

    date = draft.get("date")
    return LeagueSettings(
        name=(settings.get("name") or "").strip() or None,
        size=int(size) if size else None,
        rounds=rounds,
        roster_need=need,
        bench_slots=bench,
        draft_at=int(date) if date else None,
        draft_type=draft.get("type"),
    )


def parse_draft_detail(payload: dict) -> DraftSnapshot:
    """Turn a raw `mDraftDetail` response into a snapshot.

    `draftDetail.drafted` is deliberately ignored. It reports whether the draft
    has *finished*, and gating on it is what makes the espn_api wrapper blind to
    a draft in progress.
    """
    raw = (payload.get("draftDetail") or {}).get("picks") or []
    slots = []
    for item in raw:
        try:
            overall = int(item.get("overallPickNumber"))
        except (TypeError, ValueError):
            continue
        slots.append((overall, item))
    slots.sort(key=lambda s: s[0])

    order, picks = [], []
    for overall, item in slots:
        team_id = int(item.get("teamId") or 0)
        order.append(team_id)
        player_id = int(item.get("playerId", UNFILLED))
        if player_id == UNFILLED:
            continue
        picks.append(EspnPick(
            overall_pick=overall,
            round_id=int(item.get("roundId") or 0),
            round_pick=int(item.get("roundPickNumber") or 0),
            team_id=team_id,
            player_id=player_id,
            # Autodrafted picks carry no `memberId`, which is exactly why
            # ownership is decided by team_id (see resolve_my_team_id).
            autodrafted=bool(item.get("autoDraftTypeId")),
            keeper=bool(item.get("keeper")),
        ))

    total = len(slots)
    teams = len({t for t in order if t}) or 1
    rounds = (total // teams) if teams else 0
    return DraftSnapshot(
        order=tuple(order), picks=tuple(picks),
        total_slots=total, teams=teams, rounds=rounds,
    )


def _norm_owner(value) -> str:
    """ESPN is inconsistent about brace casing between the cookie and the JSON."""
    return (value or "").strip().strip("{}").lower()


def resolve_my_team_id(payload: dict, swid: str) -> int | None:
    """Which team belongs to the holder of this SWID.

    Matches on membership of `teams[].owners`, not on `primaryOwner`: a
    co-owned team lists several owner ids and the primary one may be somebody
    else, so a primaryOwner test would silently resolve to the wrong team.
    """
    target = _norm_owner(swid)
    if not target:
        return None
    for team in payload.get("teams") or []:
        owners = team.get("owners") or []
        if any(_norm_owner(o) == target for o in owners):
            try:
                return int(team.get("id"))
            except (TypeError, ValueError):
                return None
    return None


def team_display(payload: dict, team_id: int | None) -> dict:
    for team in payload.get("teams") or []:
        if team_id is not None and team.get("id") == team_id:
            name = team.get("name") or " ".join(
                filter(None, [team.get("location"), team.get("nickname")])
            ).strip()
            return {"id": team_id, "name": name or f"Team {team_id}",
                    "abbrev": team.get("abbrev")}
    return {"id": team_id, "name": None, "abbrev": None}


def all_teams(payload: dict) -> list[dict]:
    """Every team in the league, so the draft order can be shown by name.

    The order itself is a list of team ids; without this it would render as
    fourteen anonymous numbers.
    """
    out = []
    for team in payload.get("teams") or []:
        name = team.get("name") or " ".join(
            filter(None, [team.get("location"), team.get("nickname")])
        ).strip()
        try:
            out.append({"id": int(team.get("id")), "name": name or f"Team {team.get('id')}",
                        "abbrev": team.get("abbrev")})
        except (TypeError, ValueError):
            continue
    return out


def my_pick_numbers(snapshot: DraftSnapshot, my_team_id: int | None) -> list[int]:
    if my_team_id is None:
        return []
    return [i + 1 for i, team in enumerate(snapshot.order) if team == my_team_id]


def derive_pick_context(snapshot: DraftSnapshot, my_team_id: int | None) -> dict:
    """Where the draft is, and which picks the board should reason about.

    Mirrors the semantics the frontend already used: `this_turn` is the pick
    being decided, `next_pick` is the one after it - the "grab them now or will
    they last?" reference. Both fall back to `current_pick` once your picks run
    out, so `availability_pressure` gets a sane number rather than raising.

    This replaces the frontend's snake-draft arithmetic in live mode because it
    is read from the real order, and so survives traded picks, keepers, and any
    draft that isn't a pure snake.
    """
    mine = my_pick_numbers(snapshot, my_team_id)
    current = snapshot.current_pick
    if current is None:
        return {"current_pick": None, "this_turn": None, "next_pick": None,
                "is_my_turn": False, "my_picks": mine, "complete": True,
                "picks_until_my_turn": None, "on_the_clock": None}

    upcoming = [p for p in mine if p >= current]
    this_turn = upcoming[0] if upcoming else current
    later = [p for p in mine if p > this_turn]
    next_pick = later[0] if later else this_turn
    on_clock = snapshot.order[current - 1] if current <= len(snapshot.order) else None
    return {
        "current_pick": current,
        "this_turn": this_turn,
        "next_pick": next_pick,
        "is_my_turn": current in set(mine),
        "my_picks": mine,
        "complete": False,
        "picks_until_my_turn": max(this_turn - current, 0),
        "on_the_clock": {"team_id": on_clock, "is_me": on_clock == my_team_id},
    }


# ---------------------------------------------------------------------------
# player lookup
# ---------------------------------------------------------------------------

def resolve_players(engine, year: int, player_ids) -> dict:
    """`player_id -> (name, position)` for the candidate pool.

    ESPN's pick payload carries only a player id. `lineupSlotId` is on it but is
    the roster slot the player was placed in, not their position (98 of 224
    picks in 2025 were slot 20, the bench), so it must not be used here.

    Falls back to `players` for a name when the projection pool doesn't have
    them - a player can be drafted who was never a candidate. Position stays
    None in that case, which the session records as unresolved.
    """
    ids = [int(p) for p in set(player_ids)]
    if not ids:
        return {}
    out: dict[int, tuple] = {}
    placeholders = ",".join(str(i) for i in ids)

    rows = engine.connect().execute(text(
        f"SELECT player_id, player_name, position FROM next_season_projections "
        f"WHERE year = {int(year)} AND player_id IN ({placeholders})"
    )).fetchall()
    for pid, name, pos in rows:
        out[int(pid)] = (name, pos)

    missing = [i for i in ids if i not in out]
    if missing:
        try:
            rows = engine.connect().execute(text(
                "SELECT player_id, player_name FROM players "
                f"WHERE player_id IN ({','.join(str(i) for i in missing)})"
            )).fetchall()
            for pid, name in rows:
                out[int(pid)] = (name, None)
        except Exception:
            pass  # `players` is optional; a missing name is not fatal
    return out


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------

class EspnDraftClient:
    """Fetches draft state, with ETag caching and safe error mapping.

    `fetch` is injectable so the replay test can drive the whole pipeline from a
    fixture without mocking HTTP.
    """

    def __init__(self, creds: EspnCredentials, year: int, fetch=None, timeout: float = 6.0,
                 league_id: str | None = None):
        self.creds = creds
        self.year = year
        # Which league this client talks to. Overrides the credentials' default
        # so one set of cookies can serve several leagues - keyword-with-default
        # so existing positional construction is unaffected.
        self.league_id = str(league_id or creds.league_id or "")
        self.timeout = timeout
        self._fetch = fetch or self._http_fetch
        self._session = requests.Session() if fetch is None else None
        self._etags: dict[str, str] = {}
        self._cached: dict[str, dict] = {}

    def _url(self) -> str:
        return f"{BASE}/seasons/{self.year}/segments/0/leagues/{self.league_id}"

    def _http_fetch(self, view: str) -> dict | None:
        """Returns the payload, or None for 304 Not Modified."""
        headers = {}
        if view in self._etags:
            headers["If-None-Match"] = self._etags[view]
        try:
            resp = self._session.get(
                self._url(),
                params={"view": view},
                cookies={"SWID": self.creds.swid, "espn_s2": self.creds.espn_s2},
                headers=headers,
                timeout=self.timeout,
            )
        except requests.RequestException:
            # `from None`: the original exception can carry the request object,
            # and the request headers are the cookies.
            raise EspnUnavailable(UNAVAILABLE_MESSAGE) from None

        if resp.status_code == 304:
            return None
        if resp.status_code in (401, 403):
            # Constant message. Never resp.text, never the credentials.
            raise EspnAuthError(AUTH_MESSAGE)
        if resp.status_code >= 400:
            raise EspnUnavailable(UNAVAILABLE_MESSAGE)

        etag = resp.headers.get("ETag")
        if etag:
            self._etags[view] = etag
        try:
            return resp.json()
        except ValueError:
            raise EspnUnavailable(UNAVAILABLE_MESSAGE) from None

    def fetch(self, view: str) -> dict | None:
        payload = self._fetch(view)
        if payload is None:
            return None
        self._cached[view] = payload
        return payload

    def draft_snapshot(self) -> DraftSnapshot | None:
        """None means "unchanged since last poll" - reuse the previous snapshot."""
        payload = self.fetch("mDraftDetail")
        if payload is None:
            return None
        return parse_draft_detail(payload)

    def team_payload(self) -> dict:
        return self.fetch("mTeam") or self._cached.get("mTeam") or {}

    def settings(self) -> LeagueSettings:
        payload = self.fetch("mSettings") or self._cached.get("mSettings") or {}
        return parse_settings(payload)


def list_leagues(creds: EspnCredentials, year: int, fetch=None) -> list[LeagueRef]:
    """Every football league these credentials can reach in `year`.

    Uses ESPN's "fan" endpoint, which is keyed on the SWID rather than on a
    league - so the user never has to find a league id. Injectable `fetch` for
    the same reason as the draft client: the tests drive it from a fixture.

    Errors are mapped the same way as everywhere else in this module, because
    this call carries the cookies too and its failures must never quote them.
    """
    if fetch is not None:
        payload = fetch()
    else:
        try:
            resp = requests.get(
                f"{FAN_BASE}/{creds.swid}",
                params=FAN_PARAMS,
                cookies={"SWID": creds.swid, "espn_s2": creds.espn_s2},
                timeout=8.0,
            )
        except requests.RequestException:
            raise EspnUnavailable(UNAVAILABLE_MESSAGE) from None
        # The SWID is part of the path here, so a 404 means "no such fan" -
        # a bad SWID, not a network problem. Reporting it as unreachable would
        # send someone debugging their wifi instead of their cookies.
        if resp.status_code in (401, 403, 404):
            raise EspnAuthError(AUTH_MESSAGE)
        if resp.status_code >= 400:
            raise EspnUnavailable(UNAVAILABLE_MESSAGE)
        try:
            payload = resp.json()
        except ValueError:
            raise EspnUnavailable(UNAVAILABLE_MESSAGE) from None
    return parse_fan_leagues(payload, year)


# ---------------------------------------------------------------------------
# applying a snapshot to a session
# ---------------------------------------------------------------------------

@dataclass
class DraftSync:
    """Keeps a `DraftSession` in step with what ESPN says has been drafted.

    ESPN is the source of truth while connected. Because it returns the whole
    pick list every time, reconciliation needs exactly two paths:

    - the completed picks still start with everything already applied, so only
      the tail is new; or
    - they don't, in which case *something* disagrees - a pick was undone, a
      slot changed hands, picks arrived out of order, or the user clicked Draft
      manually - and the session is rebuilt by replaying every pick.

    Collapsing every anomaly into "rebuild" is what keeps this tractable: 224
    dict updates costs nothing, and there is no separate code path for
    duplicates, ordering, or corrections that could be wrong on draft night.
    """

    session_id: str
    client: EspnDraftClient
    engine: object
    year: int
    my_team_id: int | None = None
    team: dict = field(default_factory=dict)
    # Which league this session is attached to. Carried here rather than on the
    # DraftSession so the session stays usable with no ESPN at all - and read
    # by /recommend to decide whether the persisted league bias applies.
    league_id: str | None = None
    settings: "LeagueSettings | None" = None

    snapshot: DraftSnapshot | None = None
    applied: list = field(default_factory=list)     # [(overall_pick, player_id)]
    unresolved: list = field(default_factory=list)
    version: int = 0
    rebuilt: bool = False
    last_new: list = field(default_factory=list)
    duplicates_skipped: int = 0

    # Poll bookkeeping. `status` is what the UI renders; `last_ok` drives the
    # "last synced Ns ago" chip and the throttle.
    status: str = "ok"
    message: str | None = None
    last_ok: float = 0.0
    last_attempt: float = 0.0
    failures: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)

    # ---- helpers ----

    def _names(self, snapshot: DraftSnapshot) -> dict:
        return resolve_players(self.engine, self.year, [p.player_id for p in snapshot.picks])

    def _apply_one(self, session: DraftSession, pick: EspnPick, lookup: dict) -> dict:
        name, position = lookup.get(pick.player_id, (None, None))
        filled = session.pick(
            pick.player_id, position,
            mine=(pick.team_id == self.my_team_id),
            player_name=name,
            overall_pick=pick.overall_pick,
        )
        entry = {
            "overall_pick": pick.overall_pick,
            "round": pick.round_id,
            "team_id": pick.team_id,
            "is_me": pick.team_id == self.my_team_id,
            "player_id": pick.player_id,
            "player_name": name,
            "position": position,
            "filled_slot": filled,
            "autodrafted": pick.autodrafted,
            "resolved": position is not None,
        }
        if position is None:
            self.unresolved.append({"overall_pick": pick.overall_pick,
                                    "player_id": pick.player_id})
        return entry

    def _rebuild(self, session: DraftSession, snapshot: DraftSnapshot, lookup: dict) -> DraftSession:
        fresh = DraftSession(session.teams, session.roster_need, rounds=session.rounds)
        self.unresolved = []
        for pick in snapshot.picks:
            self._apply_one(fresh, pick, lookup)
        self.applied = [(p.overall_pick, p.player_id) for p in snapshot.picks]
        return fresh

    def apply(self, session: DraftSession, snapshot: DraftSnapshot) -> tuple[DraftSession, list]:
        """Bring `session` in line with `snapshot`. Returns (session, new picks)."""
        lookup = self._names(snapshot)
        incoming = list(snapshot.picks)  # already sorted by overall_pick

        prefix_ok = (
            len(incoming) >= len(self.applied)
            and all(
                (incoming[i].overall_pick, incoming[i].player_id) == self.applied[i]
                for i in range(len(self.applied))
            )
        )

        if not prefix_ok:
            session = self._rebuild(session, snapshot, lookup)
            self.rebuilt = True
            self.version += 1
            self.last_new = []
            return session, []

        self.rebuilt = False
        already = {n for n, _ in self.applied}
        new_entries = []
        for pick in incoming[len(self.applied):]:
            # Belt and braces: the prefix check is the real guard, but
            # DraftSession.pick double-counts a roster slot if a pick is ever
            # replayed, so never call it twice for the same slot.
            if pick.overall_pick in already:
                self.duplicates_skipped += 1
                continue
            new_entries.append(self._apply_one(session, pick, lookup))
            self.applied.append((pick.overall_pick, pick.player_id))

        if new_entries:
            self.version += 1
        self.last_new = new_entries
        return session, new_entries

    def context(self) -> dict:
        if self.snapshot is None:
            return {"current_pick": None, "this_turn": None, "next_pick": None,
                    "is_my_turn": False, "my_picks": [], "complete": False,
                    "picks_until_my_turn": None, "on_the_clock": None}
        return derive_pick_context(self.snapshot, self.my_team_id)


# Parallel to SESSIONS in src/state.py, deliberately not stored on the session
# itself: a DraftSession must stay fully usable with no ESPN connection at all.
ESPN_SYNCS: dict[str, DraftSync] = {}
