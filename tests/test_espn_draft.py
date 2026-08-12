"""Tests for live ESPN draft sync.

This code runs once a year, unattended, while the user is on the clock and has
no attention to spare. A silent failure on draft night is unrecoverable, so
most of what follows pins a specific way this could quietly do nothing.

The replay drives the real 2025 draft through the whole pipeline pick by pick.
The fixture is generated from the `drafts` table by
`notebooks/export_draft_fixture.py`, which is why it contains no member ids.
"""
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import text

from src.espn_draft import (
    AUTH_MESSAGE,
    UNFILLED,
    DraftSync,
    EspnAuthError,
    EspnCredentials,
    EspnDraftClient,
    EspnUnavailable,
    derive_pick_context,
    list_leagues,
    parse_draft_detail,
    parse_fan_leagues,
    parse_settings,
    resolve_my_team_id,
    team_display,
)
from src.state import DraftSession

FIXTURES = Path(__file__).parent / "fixtures"
MY_TEAM = 8
ROSTER_NEED = {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DST": 1}


@pytest.fixture(scope="module")
def draft_payload():
    return json.loads((FIXTURES / "espn_draft_2025.json").read_text())


@pytest.fixture(scope="module")
def mteam_payload():
    return json.loads((FIXTURES / "espn_mteam.json").read_text())


@pytest.fixture(scope="module")
def engine(draft_payload):
    """A throwaway database holding exactly the players the 2025 draft used.

    Built from the fixture's own `_players` map rather than from
    `data/fantasy_data.db`, so the replay proves the pipeline rather than the
    contents of whatever database happens to be on disk — and so it keeps
    working when conftest.py swaps `DATABASE_URL` for its own fixture.
    """
    from sqlalchemy import create_engine

    eng = create_engine("sqlite://")  # in-memory, this connection only
    with eng.begin() as conn:
        conn.execute(text(
            "CREATE TABLE next_season_projections "
            "(player_id INTEGER, player_name TEXT, position TEXT, year INTEGER)"
        ))
        rows = [{"pid": int(pid), "name": name, "pos": pos, "yr": 2026}
                for pid, (name, pos) in draft_payload["_players"].items()]
        conn.execute(
            text("INSERT INTO next_season_projections VALUES (:pid, :name, :pos, :yr)"),
            rows,
        )
    return eng


def masked(payload: dict, upto: int) -> dict:
    """The payload as ESPN would serve it after `upto` picks.

    Every later slot goes back to `playerId: -1` — which is what a real
    in-progress response looks like, slots and all.
    """
    out = copy.deepcopy(payload)
    for p in out["draftDetail"]["picks"]:
        if p["overallPickNumber"] > upto:
            p["playerId"] = UNFILLED
    return out


def make_session():
    return DraftSession(teams=14, roster_need=dict(ROSTER_NEED), rounds=16)


def make_sync(engine, client=None):
    return DraftSync(session_id="t", client=client, engine=engine, year=2026,
                     my_team_id=MY_TEAM)


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------

class TestParsing:
    def test_pre_draft_has_every_slot_but_no_picks(self, draft_payload):
        """The single most dangerous assumption in this feature.

        ESPN's pre-draft response is NOT an empty list — it carries all 224
        slots with playerId -1. Code testing `if picks:` or `len(picks)` reads
        that as a finished draft.
        """
        snap = parse_draft_detail(masked(draft_payload, 0))
        assert snap.total_slots == 224
        assert snap.picks == ()
        assert snap.complete is False
        assert snap.current_pick == 1

    def test_drafted_flag_is_ignored(self, draft_payload):
        """`draftDetail.drafted` reports completion, not "are there picks".

        Gating on it is what makes espn_api's wrapper blind for the entire
        duration of a live draft. The fixture sets it True deliberately.
        """
        payload = masked(draft_payload, 0)
        assert payload["draftDetail"]["drafted"] is True
        assert parse_draft_detail(payload).picks == ()

    def test_full_payload_parses_every_pick(self, draft_payload):
        snap = parse_draft_detail(draft_payload)
        assert len(snap.picks) == 224
        assert snap.complete is True
        assert snap.current_pick is None
        assert snap.teams == 14 and snap.rounds == 16

    def test_order_matches_round_one_pick_order(self, draft_payload):
        snap = parse_draft_detail(draft_payload)
        declared = draft_payload["settings"]["draftSettings"]["pickOrder"]
        assert list(snap.order[:14]) == declared

    def test_picks_come_back_sorted_even_when_payload_is_not(self, draft_payload):
        shuffled = copy.deepcopy(draft_payload)
        shuffled["draftDetail"]["picks"].reverse()
        snap = parse_draft_detail(shuffled)
        nums = [p.overall_pick for p in snap.picks]
        assert nums == sorted(nums)

    def test_current_pick_is_lowest_unfilled_not_a_count(self, draft_payload):
        """Keepers and commissioner edits fill slots out of order; a count-based
        current_pick then mis-reports the clock for the rest of the draft."""
        payload = masked(draft_payload, 0)
        for p in payload["draftDetail"]["picks"]:
            if p["overallPickNumber"] == 5:
                p["playerId"] = 999001  # a keeper in slot 5, nothing before it
        snap = parse_draft_detail(payload)
        assert len(snap.picks) == 1
        assert snap.current_pick == 1

    def test_autodrafted_flag_survives(self, draft_payload):
        snap = parse_draft_detail(draft_payload)
        assert any(p.autodrafted for p in snap.picks)


# ---------------------------------------------------------------------------
# ownership
# ---------------------------------------------------------------------------

class TestOwnership:
    def test_resolves_via_owners_membership_not_primary_owner(self, mteam_payload):
        """Team 8 is co-owned and its primaryOwner is somebody else.

        A primaryOwner test would silently resolve to the wrong team, and every
        "is this my pick?" decision after that would be wrong.
        """
        swid = "{AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB}"
        assert mteam_payload["teams"][1]["primaryOwner"] != swid
        assert resolve_my_team_id(mteam_payload, swid) == 8

    @pytest.mark.parametrize("swid", [
        "{AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB}",
        "AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB",
        "{REDACTED-ESPN-MEMBER-ID}",
        "  {AAAAAAAA-1111-2222-3333-BBBBBBBBBBBB}  ",
    ])
    def test_swid_matches_across_brace_and_case_spellings(self, mteam_payload, swid):
        # ESPN is inconsistent about brace casing between the cookie and the JSON.
        assert resolve_my_team_id(mteam_payload, swid) == 8

    def test_unknown_swid_returns_none(self, mteam_payload):
        assert resolve_my_team_id(mteam_payload, "{REDACTED-ESPN-MEMBER-ID}") is None

    def test_empty_swid_returns_none(self, mteam_payload):
        assert resolve_my_team_id(mteam_payload, "") is None

    def test_team_display_reads_the_name(self, mteam_payload):
        assert team_display(mteam_payload, 8)["name"] == "Flying Eagles"
        assert team_display(mteam_payload, 8)["abbrev"] == "EGL"

    def test_autodrafted_picks_still_attribute_to_a_team(self, draft_payload):
        """Autodrafted picks carry no memberId — 74 of 224 in 2025.

        Deciding ownership by memberId would drop a third of the draft.
        """
        snap = parse_draft_detail(draft_payload)
        auto_mine = [p for p in snap.picks if p.autodrafted and p.team_id == MY_TEAM]
        assert all("memberId" not in p.__dict__ for p in snap.picks)
        # The point: they're attributable regardless.
        assert all(p.team_id for p in snap.picks)
        assert isinstance(auto_mine, list)


# ---------------------------------------------------------------------------
# pick context
# ---------------------------------------------------------------------------

class TestPickContext:
    def test_my_picks_are_the_slots_assigned_to_my_team(self, draft_payload):
        snap = parse_draft_detail(masked(draft_payload, 0))
        ctx = derive_pick_context(snap, MY_TEAM)
        assert len(ctx["my_picks"]) == 16
        assert ctx["my_picks"] == sorted(ctx["my_picks"])

    def test_next_pick_is_the_turn_after_this_one(self, draft_payload):
        snap = parse_draft_detail(masked(draft_payload, 0))
        ctx = derive_pick_context(snap, MY_TEAM)
        assert ctx["current_pick"] == 1
        assert ctx["this_turn"] == ctx["my_picks"][0]
        assert ctx["next_pick"] == ctx["my_picks"][1]
        assert ctx["next_pick"] > ctx["this_turn"]

    def test_ordering_invariant_holds_at_every_step(self, draft_payload):
        for upto in range(0, 224, 17):
            snap = parse_draft_detail(masked(draft_payload, upto))
            ctx = derive_pick_context(snap, MY_TEAM)
            if ctx["complete"]:
                continue
            assert ctx["current_pick"] <= ctx["this_turn"] <= ctx["next_pick"]

    def test_is_my_turn_only_on_my_pick_numbers(self, draft_payload):
        full = parse_draft_detail(masked(draft_payload, 0))
        mine = set(derive_pick_context(full, MY_TEAM)["my_picks"])
        for upto in (0, 3, 24, 31, 52):
            snap = parse_draft_detail(masked(draft_payload, upto))
            ctx = derive_pick_context(snap, MY_TEAM)
            assert ctx["is_my_turn"] == (ctx["current_pick"] in mine)

    def test_last_pick_degrades_instead_of_raising(self, draft_payload):
        """Past your final turn there is no "next pick"; it falls back to this
        one so availability_pressure gets a neutral value rather than blowing up."""
        snap = parse_draft_detail(masked(draft_payload, 223))
        ctx = derive_pick_context(snap, MY_TEAM)
        assert ctx["next_pick"] == ctx["this_turn"]

    def test_complete_draft_reports_no_clock(self, draft_payload):
        ctx = derive_pick_context(parse_draft_detail(draft_payload), MY_TEAM)
        assert ctx["complete"] is True and ctx["current_pick"] is None

    def test_unknown_team_has_no_picks_and_never_its_turn(self, draft_payload):
        snap = parse_draft_detail(masked(draft_payload, 0))
        ctx = derive_pick_context(snap, None)
        assert ctx["my_picks"] == [] and ctx["is_my_turn"] is False


# ---------------------------------------------------------------------------
# client
# ---------------------------------------------------------------------------

class TestClient:
    def _client(self, fetch):
        return EspnDraftClient(EspnCredentials("1", "swid", "s2"), 2026, fetch=fetch)

    def test_304_means_unchanged_not_empty(self, draft_payload):
        client = self._client(lambda view: None)
        assert client.draft_snapshot() is None

    def test_snapshot_parses_through_the_client(self, draft_payload):
        client = self._client(lambda view: draft_payload)
        assert len(client.draft_snapshot().picks) == 224

    def test_auth_error_message_is_a_constant(self):
        def fetch(view):
            raise EspnAuthError(AUTH_MESSAGE)
        with pytest.raises(EspnAuthError) as exc:
            self._client(fetch).draft_snapshot()
        assert str(exc.value) == AUTH_MESSAGE

    def test_auth_error_never_carries_credentials(self):
        """espn_api's own exception interpolates espn_s2 and swid into its
        message. Ours must never be able to."""
        secret_swid, secret_s2 = "SWID-SENTINEL-9931", "S2-SENTINEL-4471"

        def fetch(view):
            raise EspnAuthError(AUTH_MESSAGE)
        client = EspnDraftClient(EspnCredentials("1", secret_swid, secret_s2), 2026, fetch=fetch)
        with pytest.raises(EspnAuthError) as exc:
            client.draft_snapshot()
        assert secret_swid not in str(exc.value)
        assert secret_s2 not in str(exc.value)

    def test_network_failure_maps_to_unavailable(self):
        def fetch(view):
            raise EspnUnavailable("Could not reach ESPN.")
        with pytest.raises(EspnUnavailable):
            self._client(fetch).draft_snapshot()


class TestNoEspnApiImport:
    def test_importing_espn_draft_does_not_import_espn_api(self):
        """Structural guarantee, not a promise to be careful.

        espn_api's ESPNAccessDenied formats the session cookie into its message,
        so the safest thing is for that module to be unreachable from anything
        that can raise into an HTTP response.
        """
        code = (
            "import sys; import src.espn_draft; "
            "sys.exit(1 if any(m == 'espn_api' or m.startswith('espn_api.') "
            "for m in sys.modules) else 0)"
        )
        repo = Path(__file__).parent.parent
        result = subprocess.run([sys.executable, "-c", code], cwd=repo, capture_output=True)
        assert result.returncode == 0, "src.espn_draft must not import espn_api"


# ---------------------------------------------------------------------------
# replay — the proof the whole pipeline works
# ---------------------------------------------------------------------------

class TestReplay:
    """Drives the real 2025 draft through DraftSync one pick at a time.

    This is the closest thing to a draft-night rehearsal that can run in CI.
    """

    def _run(self, payload, engine, step=1):
        session, sync = make_session(), make_sync(engine)
        seen = []
        # Always finish on the complete payload: a step that doesn't divide 224
        # would otherwise stop short, and the draft's last picks are exactly
        # where a bug would hide.
        stops = sorted({*range(0, 225, step), 224})
        for upto in stops:
            snap = parse_draft_detail(masked(payload, upto))
            sync.snapshot = snap
            session, new = sync.apply(session, snap)
            seen.extend(new)
        return session, sync, seen

    def test_every_pick_is_detected_exactly_once(self, draft_payload, engine):
        session, sync, seen = self._run(draft_payload, engine)
        assert len(seen) == 224
        assert [e["overall_pick"] for e in seen] == list(range(1, 225))
        assert sync.duplicates_skipped == 0
        assert len(session.drafted_ids) == 224
        assert len(session.draft_log) == 224

    def test_ownership_matches_the_real_draft(self, draft_payload, engine):
        session, sync, seen = self._run(draft_payload, engine)
        mine = [e["overall_pick"] for e in seen if e["is_me"]]
        assert len(mine) == 16
        assert session.my_picks_made == 16
        # Team 8 drafted 4th in 2025; the snake puts them at 4, 25, 32, ...
        assert mine[:4] == [4, 25, 32, 53]
        assert mine[-1] == 221

    def test_final_roster_is_full_with_the_right_bench(self, draft_payload, engine):
        session, _, _ = self._run(draft_payload, engine)
        for pos, slot in session.roster_state.items():
            assert slot["have"] == slot["need"], pos
        # 16 rounds minus 9 starting slots.
        assert session.bench_filled == 7
        assert sum(session.depth.values()) == 7

    def test_chunked_replay_gives_identical_state(self, draft_payload, engine):
        """Polling can miss picks — a poll every 5s over a 60s clock, a laptop
        that slept, a dropped request. Batches must land exactly like singles."""
        one, _, _ = self._run(draft_payload, engine, step=1)
        five, _, _ = self._run(draft_payload, engine, step=5)
        assert one.roster_state == five.roster_state
        assert one.depth == five.depth
        assert [e["player_id"] for e in one.draft_log] == [e["player_id"] for e in five.draft_log]
        assert [e["is_my_pick"] for e in one.draft_log] == [e["is_my_pick"] for e in five.draft_log]

    def test_reapplying_the_same_payload_changes_nothing(self, draft_payload, engine):
        """Most polls return an unchanged list. Re-applying must be inert —
        DraftSession.pick double-counts a roster slot if a pick is replayed."""
        session, sync = make_session(), make_sync(engine)
        snap = parse_draft_detail(draft_payload)
        session, first = sync.apply(session, snap)
        version_after_first = sync.version
        before = copy.deepcopy(session.roster_state)

        session, second = sync.apply(session, snap)
        assert second == []
        assert sync.version == version_after_first
        assert session.roster_state == before
        assert len(session.draft_log) == 224

    def test_rewind_triggers_a_rebuild(self, draft_payload, engine):
        """A commissioner undo, or a pick changing hands, makes the remote list
        shorter or different. One rebuild path handles every such case."""
        session, sync = make_session(), make_sync(engine)
        snap = parse_draft_detail(masked(draft_payload, 100))
        session, _ = sync.apply(session, snap)
        assert len(session.draft_log) == 100

        rewound = parse_draft_detail(masked(draft_payload, 95))
        session, new = sync.apply(session, rewound)
        assert sync.rebuilt is True
        assert new == []
        assert len(session.draft_log) == 95
        assert len(session.drafted_ids) == 95
        # And the roster is consistent with 95 picks, not with 100 minus 5.
        expected = sum(1 for e in session.draft_log if e["is_my_pick"])
        assert session.my_picks_made == expected

    def test_a_changed_player_at_an_existing_slot_rebuilds(self, draft_payload, engine):
        session, sync = make_session(), make_sync(engine)
        session, _ = sync.apply(session, parse_draft_detail(masked(draft_payload, 50)))

        edited = masked(draft_payload, 50)
        for p in edited["draftDetail"]["picks"]:
            if p["overallPickNumber"] == 12:
                p["playerId"] = 999002
        session, _ = sync.apply(session, parse_draft_detail(edited))
        assert sync.rebuilt is True
        assert 999002 in session.drafted_ids
        assert len(session.draft_log) == 50

    def test_unknown_player_does_not_crash_and_is_reported(self, draft_payload, engine):
        """A drafted player who was never a candidate has no position. The pick
        must still leave the board, consume no roster slot, and be surfaced —
        a silently incomplete roster is worse than a visible gap."""
        payload = masked(draft_payload, 0)
        for p in payload["draftDetail"]["picks"]:
            if p["overallPickNumber"] == 1:
                p["playerId"] = 999999          # not in any table
                p["teamId"] = MY_TEAM           # and it's mine, the harder case

        session, sync = make_session(), make_sync(engine)
        session, new = sync.apply(session, parse_draft_detail(payload))

        assert len(new) == 1
        entry = new[0]
        assert entry["resolved"] is False
        assert entry["position"] is None
        assert entry["filled_slot"] is None
        assert 999999 in session.drafted_ids
        assert all(s["have"] == 0 for s in session.roster_state.values())
        assert sync.unresolved == [{"overall_pick": 1, "player_id": 999999}]

    def test_version_only_moves_when_something_happened(self, draft_payload, engine):
        session, sync = make_session(), make_sync(engine)
        start = sync.version
        session, _ = sync.apply(session, parse_draft_detail(masked(draft_payload, 0)))
        assert sync.version == start          # 224 empty slots is not an event
        session, _ = sync.apply(session, parse_draft_detail(masked(draft_payload, 1)))
        assert sync.version == start + 1

    def test_payload_is_json_safe(self, draft_payload, engine):
        """None must serialise as null; NaN has 500ed this API before."""
        _, _, seen = self._run(draft_payload, engine, step=8)
        blob = json.dumps(seen)
        assert "NaN" not in blob and "Infinity" not in blob


# ---------------------------------------------------------------------------
# league discovery and settings
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def fan_payload():
    return json.loads((FIXTURES / "espn_fan_leagues.json").read_text())


def settings_payload(*, date=None, size=14, name="McFL", counts=None):
    """A `view=mSettings` response, shaped like the real one.

    `counts` defaults to the standard 14-team layout every one of the user's
    leagues actually uses: QB/RB/RB/WR/WR/TE/FLEX/K/DST plus 7 bench.
    """
    draft = {"type": "SNAKE", "orderType": "DRAFT_START", "pickOrder": list(range(1, 15))}
    # The key is ABSENT when no date is set - ESPN sends no null and no
    # sentinel. That absence is the entire "not scheduled" signal.
    if date is not None:
        draft["date"] = date
    return {"settings": {
        "name": name,
        "size": size,
        "draftSettings": draft,
        "rosterSettings": {"lineupSlotCounts": counts if counts is not None else {
            "0": 1, "2": 2, "4": 2, "6": 1, "16": 1, "17": 1, "20": 7, "23": 1,
            "21": 0, "3": 0, "5": 0,
        }},
    }}


class TestFanLeagues:
    def test_finds_every_football_league_for_the_season(self, fan_payload):
        leagues = parse_fan_leagues(fan_payload, 2026)
        assert [l.name for l in leagues] == ["Sigma Fantasy", "McFL", "Sigmas"]
        assert [l.league_id for l in leagues] == ["1224888142", "780575", "197229335"]

    def test_other_sports_are_excluded(self, fan_payload):
        # The payload mixes every fantasy sport the user plays.
        assert all("Hoops" not in l.name for l in parse_fan_leagues(fan_payload, 2026))

    def test_other_seasons_are_excluded(self, fan_payload):
        assert len(parse_fan_leagues(fan_payload, 2026)) == 3
        assert [l.season for l in parse_fan_leagues(fan_payload, 2025)] == [2025]

    def test_entry_with_no_league_is_skipped_not_fatal(self, fan_payload):
        # One malformed entry shouldn't stop the picker rendering the rest.
        assert all(l.league_id for l in parse_fan_leagues(fan_payload, 2026))

    def test_duplicate_leagues_appear_once(self, fan_payload):
        ids = [l.league_id for l in parse_fan_leagues(fan_payload, 2026)]
        assert len(ids) == len(set(ids))

    def test_empty_payload_is_empty_not_an_error(self):
        assert parse_fan_leagues({}, 2026) == []


class TestLeagueSettings:
    def test_absent_date_means_not_scheduled(self):
        """McFL's actual state, and the discriminator the whole feature hangs on.

        ESPN omits the key rather than sending null, so any check subtler than
        "is it there" would get this wrong.
        """
        s = parse_settings(settings_payload(date=None))
        assert s.draft_at is None
        assert s.scheduled is False

    def test_epoch_date_means_scheduled(self):
        # Sigma Fantasy: 2026-08-24 23:00 UTC.
        s = parse_settings(settings_payload(date=1787612400000))
        assert s.draft_at == 1787612400000
        assert s.scheduled is True

    def test_roster_need_matches_the_projects_shape(self):
        s = parse_settings(settings_payload())
        assert s.roster_need == {"QB": 1, "RB": 2, "WR": 2, "TE": 1, "FLEX": 1, "K": 1, "DST": 1}

    def test_bench_and_ir_are_not_starting_slots(self):
        """roster_need drives replacement level, which is defined by who starts."""
        s = parse_settings(settings_payload())
        assert "BE" not in s.roster_need and "IR" not in s.roster_need
        assert s.bench_slots == 7

    def test_rounds_are_starters_plus_bench(self):
        s = parse_settings(settings_payload())
        assert sum(s.roster_need.values()) == 9
        assert s.rounds == 16

    def test_a_two_quarterback_league_is_read_not_assumed(self):
        """The reason this is parsed rather than hardcoded: a different lineup
        changes replacement level, and therefore every VORP on the board."""
        counts = {"0": 2, "2": 2, "4": 2, "6": 1, "16": 1, "17": 1, "20": 7, "23": 1}
        s = parse_settings(settings_payload(counts=counts))
        assert s.roster_need["QB"] == 2
        assert sum(s.roster_need.values()) == 10

    def test_name_and_size_come_through(self):
        s = parse_settings(settings_payload(name="Sigma Fantasy ", size=12))
        assert s.name == "Sigma Fantasy"  # trailing space in the real payload
        assert s.size == 12

    def test_empty_payload_degrades_without_raising(self):
        s = parse_settings({})
        assert s.roster_need == {} and s.scheduled is False and s.size is None


class TestListLeagues:
    def test_uses_the_injected_fetch(self, fan_payload):
        creds = EspnCredentials("", "swid", "s2")
        leagues = list_leagues(creds, 2026, fetch=lambda: fan_payload)
        assert len(leagues) == 3

    def test_credentials_without_a_league_id_are_valid(self, fan_payload):
        """LEAGUE_ID is only a default now - discovery is what replaces it."""
        creds = EspnCredentials("", "swid", "s2")
        assert list_leagues(creds, 2026, fetch=lambda: fan_payload)


class TestClientLeagueOverride:
    def test_league_id_overrides_the_credential_default(self):
        creds = EspnCredentials("780575", "swid", "s2")
        client = EspnDraftClient(creds, 2026, fetch=lambda v: {}, league_id="1224888142")
        assert "/leagues/1224888142" in client._url()

    def test_falls_back_to_the_credential_league(self):
        creds = EspnCredentials("780575", "swid", "s2")
        client = EspnDraftClient(creds, 2026, fetch=lambda v: {})
        assert "/leagues/780575" in client._url()
