"""Endpoint contract for live ESPN draft sync.

The module-level tests in `test_espn_draft.py` prove the engine; these prove
the wiring — that connecting, polling and disconnecting behave, that a failed
sync degrades instead of taking the board down, and that no credential can
reach a response body.
"""
import copy
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import src.api as api
import src.espn_draft as espn_draft
from src.api import app
from src.espn_draft import (
    AUTH_MESSAGE,
    UNFILLED,
    EspnAuthError,
    EspnCredentials,
    EspnDraftClient,
    EspnUnavailable,
)

import src.auth as auth
import src.leagues as leagues_cache

FIXTURES = Path(__file__).parent / "fixtures"
MY_TEAM = 8

SWID_SENTINEL = "{SWID-SENTINEL-0000-0000-000000000000}"
S2_SENTINEL = "S2-SENTINEL-abcdef0123456789"


@pytest.fixture(autouse=True)
def _fresh_league_cache():
    """Reachability is cached per user; tests must not inherit each other's."""
    leagues_cache.clear()
    yield
    leagues_cache.clear()


@pytest.fixture
def client():
    """A signed-in client carrying the sentinel credentials.

    These used to come from a `load_credentials` stub, i.e. from the operator's
    `.env`. That path no longer exists: an unauthenticated request gets a 401
    rather than the deployer's ESPN account, so the tests sign in like a real
    user and the ownership checks are exercised rather than bypassed.
    """
    token = auth.issue(SWID_SENTINEL, S2_SENTINEL)
    return TestClient(app, headers={"X-Device-Token": token})


@pytest.fixture
def other_client():
    """A second signed-in user, for proving one cannot reach the other's board."""
    token = auth.issue("{OTHER-9999-8888-7777-666666666666}", "S2-OTHER-abcdef")
    return TestClient(app, headers={"X-Device-Token": token})


def _allow_leagues(monkeypatch, *league_ids):
    """Make `assert_league` accept these ids without calling ESPN.

    Clears the cache first: reachability is memoised per user, so a test that
    changes which leagues exist would otherwise be answered from the previous
    call's set. In production the TTL handles this; here it has to be explicit.
    """
    leagues_cache.clear()
    refs = [espn_draft.LeagueRef(league_id=str(i), name=f"L{i}", season=2026)
            for i in league_ids]
    monkeypatch.setattr(leagues_cache, "list_leagues", lambda creds, year: refs)


@pytest.fixture(scope="module")
def draft_payload():
    return json.loads((FIXTURES / "espn_draft_2025.json").read_text())


@pytest.fixture(scope="module")
def mteam_payload():
    return json.loads((FIXTURES / "espn_mteam.json").read_text())


def masked(payload, upto):
    out = copy.deepcopy(payload)
    for p in out["draftDetail"]["picks"]:
        if p["overallPickNumber"] > upto:
            p["playerId"] = UNFILLED
    return out


class FakeEspn:
    """Stands in for the network. `upto` is how far the draft has got."""

    def __init__(self, draft_payload, mteam_payload, upto=0):
        self.draft_payload = draft_payload
        self.mteam_payload = mteam_payload
        self.upto = upto
        self.calls = 0
        self.raise_on_draft = None

    def fetch(self, view):
        if view == "mTeam":
            return self.mteam_payload
        self.calls += 1
        if self.raise_on_draft:
            raise self.raise_on_draft
        return masked(self.draft_payload, self.upto)


@pytest.fixture
def connected(monkeypatch, draft_payload, mteam_payload, client):
    """A session wired to a fake ESPN, with the throttle disabled by default."""
    fake = FakeEspn(draft_payload, mteam_payload)

    # The synthetic mTeam fixture owns its SWID, so resolution has to be told
    # which one to look for; ownership matching itself is tested in
    # test_espn_draft.py against every brace/case spelling.
    monkeypatch.setattr(api, "resolve_my_team_id", lambda payload, swid: MY_TEAM)
    monkeypatch.setattr(
        api, "EspnDraftClient",
        lambda creds, year, **kw: EspnDraftClient(creds, year, fetch=fake.fetch),
    )
    monkeypatch.setattr(api, "_MIN_POLL_SECONDS", 0.0)

    # conftest.py points the app at a fixture database that has none of the
    # 2025 players, so resolve players from the draft fixture's own map. This
    # keeps the roster assertions meaningful instead of every pick coming back
    # unresolved (which is separately tested in test_espn_draft.py).
    lookup = {int(pid): (name, pos)
              for pid, (name, pos) in draft_payload["_players"].items()}
    monkeypatch.setattr(
        espn_draft, "resolve_players",
        lambda engine, year, ids: {i: lookup[i] for i in set(ids) if i in lookup},
    )

    sid = client.post("/session", json={"teams": 14, "rounds": 16}).json()["session_id"]
    res = client.post("/espn/connect", json={"session_id": sid})
    assert res.status_code == 200, res.text
    return sid, fake, res.json()


class TestConnect:
    def test_reports_the_draft_shape_before_a_single_pick(self, connected):
        _, _, body = connected
        assert body["connected"] is True
        assert body["total_slots"] == 224
        assert body["teams"] == 14 and body["rounds"] == 16
        assert body["current_pick"] == 1
        assert body["complete"] is False
        assert len(body["my_picks"]) == 16
        assert body["draft_log"] == []

    def test_pick_order_is_known_in_advance(self, connected):
        _, _, body = connected
        assert len(body["pick_order"]) == 224
        # Your own picks are exactly the slots assigned to your team.
        mine = [i + 1 for i, t in enumerate(body["pick_order"]) if t == MY_TEAM]
        assert mine == body["my_picks"]

    def test_unknown_session_is_404(self, client):
        assert client.post("/espn/connect", json={"session_id": "nope"}).status_code == 404

    def test_auth_failure_is_502_with_a_constant_message(self, monkeypatch, client):
        """ESPN rejecting the stored cookies, not a missing sign-in."""
        def boom(*a, **k):
            raise EspnAuthError(AUTH_MESSAGE)
        monkeypatch.setattr(api, "EspnDraftClient", boom)
        sid = client.post("/session", json={}).json()["session_id"]
        res = client.post("/espn/connect", json={"session_id": sid})
        assert res.status_code == 502
        assert res.json()["detail"] == AUTH_MESSAGE

    def test_unreachable_is_503(self, monkeypatch, client):
        def boom(*a, **k):
            raise EspnUnavailable("Could not reach ESPN.")
        monkeypatch.setattr(api, "EspnDraftClient", boom)
        sid = client.post("/session", json={}).json()["session_id"]
        assert client.post("/espn/connect", json={"session_id": sid}).status_code == 503


class TestSync:
    def test_picks_apply_without_anyone_clicking(self, connected, client):
        sid, fake, _ = connected
        fake.upto = 3
        body = client.get(f"/espn/sync/{sid}").json()
        assert [p["overall_pick"] for p in body["new_picks"]] == [1, 2, 3]
        assert body["drafted_count"] == 3
        assert body["current_pick"] == 4

    def test_my_own_picks_fill_my_roster(self, connected, client):
        sid, fake, _ = connected
        fake.upto = 4  # team 8 drafted 4th in 2025
        body = client.get(f"/espn/sync/{sid}").json()
        mine = [p for p in body["new_picks"] if p["is_me"]]
        assert len(mine) == 1 and mine[0]["overall_pick"] == 4
        assert mine[0]["filled_slot"] is not None
        assert sum(v["have"] for v in body["roster_state"].values()) == 1

    def test_other_teams_picks_leave_the_pool_without_touching_my_roster(self, connected, client):
        sid, fake, _ = connected
        fake.upto = 3  # none of these are team 8's
        body = client.get(f"/espn/sync/{sid}").json()
        assert all(p["is_me"] is False for p in body["new_picks"])
        assert body["drafted_count"] == 3
        assert all(v["have"] == 0 for v in body["roster_state"].values())

    def test_version_moves_only_when_something_happened(self, connected, client):
        sid, fake, _ = connected
        fake.upto = 5
        first = client.get(f"/espn/sync/{sid}").json()
        second = client.get(f"/espn/sync/{sid}").json()
        assert second["version"] == first["version"]
        assert second["new_picks"] == []
        fake.upto = 6
        third = client.get(f"/espn/sync/{sid}").json()
        assert third["version"] == first["version"] + 1

    def test_full_draft_reports_complete(self, connected, client):
        sid, fake, _ = connected
        fake.upto = 224
        body = client.get(f"/espn/sync/{sid}").json()
        assert body["complete"] is True
        assert body["current_pick"] is None
        assert body["drafted_count"] == 224
        assert body["bench_filled"] == 7

    def test_auth_failure_keeps_the_board_alive(self, connected, client):
        """The sync failed, not the request. Losing the board mid-draft because
        a cookie expired would be worse than the expiry itself."""
        sid, fake, _ = connected
        fake.upto = 2
        client.get(f"/espn/sync/{sid}")
        fake.raise_on_draft = EspnAuthError(AUTH_MESSAGE)
        res = client.get(f"/espn/sync/{sid}")
        assert res.status_code == 200
        body = res.json()
        assert body["status"] == "auth"
        assert body["message"] == AUTH_MESSAGE
        assert body["drafted_count"] == 2  # state survived

    def test_network_failure_serves_the_last_good_state(self, connected, client):
        sid, fake, _ = connected
        fake.upto = 7
        client.get(f"/espn/sync/{sid}")
        fake.raise_on_draft = EspnUnavailable("Could not reach ESPN.")
        body = client.get(f"/espn/sync/{sid}").json()
        assert body["status"] == "stale"
        assert body["drafted_count"] == 7

    def test_throttle_collapses_rapid_polls(self, monkeypatch, connected, client):
        """StrictMode double-mounts and a second tab both multiply the poll
        rate; without the throttle that lands on ESPN."""
        sid, fake, _ = connected
        monkeypatch.setattr(api, "_MIN_POLL_SECONDS", 60.0)
        before = fake.calls
        for _ in range(4):
            client.get(f"/espn/sync/{sid}")
        assert fake.calls == before  # every one served from the last snapshot

    def test_sync_without_connect_is_404(self, client):
        sid = client.post("/session", json={}).json()["session_id"]
        assert client.get(f"/espn/sync/{sid}").status_code == 404


class TestDisconnect:
    def test_keeps_the_session_and_its_picks(self, connected, client):
        sid, fake, _ = connected
        fake.upto = 10
        client.get(f"/espn/sync/{sid}")
        assert client.post("/espn/disconnect", json={"session_id": sid}).json() == {"connected": False}
        # Sync is gone...
        assert client.get(f"/espn/sync/{sid}").status_code == 404
        # ...but the draft is not: manual picks still work.
        res = client.post("/pick", json={"session_id": sid, "player_id": 999123,
                                         "position": "RB", "is_my_pick": True})
        assert res.status_code == 200
        assert len(res.json()["draft_log"]) == 11

    def test_disconnecting_when_never_connected_is_harmless(self, client):
        sid = client.post("/session", json={}).json()["session_id"]
        assert client.post("/espn/disconnect", json={"session_id": sid}).status_code == 200


class TestNoCredentialLeak:
    def test_no_response_body_contains_a_credential(self, connected, client):
        """espn_api's own exception formats espn_s2 and swid into its message.
        Nothing on this path may be able to do the same."""
        sid, fake, connect_body = connected
        fake.upto = 12
        bodies = [
            json.dumps(connect_body),
            client.get(f"/espn/sync/{sid}").text,
        ]
        fake.raise_on_draft = EspnAuthError(AUTH_MESSAGE)
        bodies.append(client.get(f"/espn/sync/{sid}").text)
        for body in bodies:
            assert SWID_SENTINEL not in body
            assert S2_SENTINEL not in body
            assert "espn_s2" not in body

    def test_sync_payload_is_json_safe(self, connected, client):
        """None must serialise as null - position, player_name and filled_slot
        are all nullable here, and NaN has 500ed this API before."""
        sid, fake, _ = connected
        fake.upto = 60
        text = client.get(f"/espn/sync/{sid}").text
        assert "NaN" not in text and "Infinity" not in text


# ---------------------------------------------------------------------------
# multi-league
# ---------------------------------------------------------------------------

FAN = json.loads((FIXTURES / "espn_fan_leagues.json").read_text())


def settings_for(name, date=None):
    draft = {"type": "SNAKE", "orderType": "DRAFT_START", "pickOrder": list(range(1, 15))}
    if date is not None:
        draft["date"] = date
    return {"settings": {
        "name": name, "size": 14, "draftSettings": draft,
        "rosterSettings": {"lineupSlotCounts": {
            "0": 1, "2": 2, "4": 2, "6": 1, "16": 1, "17": 1, "20": 7, "23": 1}},
    }}


class TestLeaguesEndpoint:
    def test_lists_every_football_league(self, monkeypatch, client):
        monkeypatch.setattr(api, "list_leagues",
                            lambda creds, year: espn_draft.parse_fan_leagues(FAN, year))
        body = client.get("/espn/leagues").json()
        assert [l["name"] for l in body["leagues"]] == ["Sigma Fantasy", "McFL", "Sigmas"]

    def test_leaks_no_credential(self, monkeypatch, client):
        monkeypatch.setattr(api, "list_leagues",
                            lambda creds, year: espn_draft.parse_fan_leagues(FAN, year))
        text = client.get("/espn/leagues").text
        assert SWID_SENTINEL not in text and S2_SENTINEL not in text

    def test_auth_failure_is_502_with_the_constant_message(self, monkeypatch, client):
        def boom(*a, **k):
            raise EspnAuthError(AUTH_MESSAGE)
        monkeypatch.setattr(api, "list_leagues", boom)
        res = client.get("/espn/leagues")
        assert res.status_code == 502 and res.json()["detail"] == AUTH_MESSAGE


class TestUnreadableSettingsAreNotUnscheduledDrafts:
    """"ESPN wouldn't tell us" and "there's no date" are different facts.

    Collapsing both into a null `draft_at` is what made a wrong espn_s2 look
    like three leagues that simply hadn't scheduled their drafts - a dead end,
    since nothing on screen pointed at the credentials.
    """

    @pytest.fixture(autouse=True)
    def _creds(self, monkeypatch):
        monkeypatch.setattr(api, "list_leagues",
                            lambda creds, year: espn_draft.parse_fan_leagues(FAN, year))

    def _client_raising(self, exc):
        class C:
            def __init__(self, *a, **k):
                pass

            def settings(self):
                raise exc
        return C

    def test_every_league_failing_is_reported_as_stale_credentials(self, monkeypatch, client):
        monkeypatch.setattr(api, "EspnDraftClient",
                            self._client_raising(EspnAuthError(AUTH_MESSAGE)))
        body = client.get("/espn/leagues").json()
        assert body["credentials_stale"] is True
        assert all(l["unreadable"] for l in body["leagues"])
        # Still listed - the SWID worked, which is precisely the confusing part.
        assert len(body["leagues"]) == 3

    def test_a_readable_league_means_the_credentials_are_fine(self, monkeypatch, client):
        class C:
            def __init__(self, creds, year, league_id=None):
                self.league_id = league_id

            def settings(self):
                if self.league_id != "1224888142":
                    raise EspnUnavailable("Could not reach ESPN.")
                return espn_draft.parse_settings(
                    settings_for("Sigma Fantasy", date=1787612400000))
        monkeypatch.setattr(api, "EspnDraftClient", C)
        body = client.get("/espn/leagues").json()
        assert body["credentials_stale"] is False
        by_id = {l["league_id"]: l for l in body["leagues"]}
        assert by_id["1224888142"]["scheduled"] is True
        assert by_id["1224888142"]["unreadable"] is False
        # The others failed to answer, which is not the same as having no date.
        assert by_id["780575"]["unreadable"] is True

    def test_a_genuinely_unscheduled_league_is_not_flagged(self, monkeypatch, client):
        class C:
            def __init__(self, *a, **k):
                pass

            def settings(self):
                return espn_draft.parse_settings(settings_for("McFL"))   # no date
        monkeypatch.setattr(api, "EspnDraftClient", C)
        body = client.get("/espn/leagues").json()
        assert body["credentials_stale"] is False
        assert all(l["unreadable"] is False for l in body["leagues"])
        assert all(l["scheduled"] is False for l in body["leagues"])


class TestBiasIsScopedToItsLeague:
    """The bias was measured on one league's drafters.

    Applying "this league reaches on Eagles" to a different league's board
    would be asserting something never measured about people we've never seen.
    """

    def _connect(self, monkeypatch, draft_payload, mteam_payload, client, league_id, name, date=None):
        fake = FakeEspn(draft_payload, mteam_payload)
        real_fetch = fake.fetch

        def fetch(view):
            if view == "mSettings":
                return settings_for(name, date)
            return real_fetch(view)

        _allow_leagues(monkeypatch, league_id)
        monkeypatch.setattr(api, "resolve_my_team_id", lambda payload, swid: MY_TEAM)
        monkeypatch.setattr(api, "EspnDraftClient",
                            lambda creds, year, **kw: EspnDraftClient(
                                creds, year, fetch=fetch, league_id=kw.get("league_id")))
        monkeypatch.setattr(api, "BIAS", {"position": {"QB": -11.1}, "pro_team": {},
                                          "player": {}, "meta": {"league_id": "780575"}})
        sid = client.post("/session", json={"teams": 14, "rounds": 16}).json()["session_id"]
        res = client.post("/espn/connect",
                          json={"session_id": sid, "league_id": league_id})
        assert res.status_code == 200, res.text
        return sid, res.json()

    def test_fitted_league_gets_the_shift(self, monkeypatch, draft_payload, mteam_payload, client):
        sid, body = self._connect(monkeypatch, draft_payload, mteam_payload, client,
                                  "780575", "McFL")
        assert body["league"]["has_history"] is True
        rows = client.post("/recommend", json={"session_id": sid, "current_pick": 1,
                                               "next_pick": 12, "topn": 50}).json()["results"]
        qb = next(r for r in rows if r["position"] == "QB")
        assert qb["bias_shift"] < -5
        assert qb["bias_reason"]

    def test_a_different_league_gets_market_adp_only(self, monkeypatch, draft_payload, mteam_payload, client):
        sid, body = self._connect(monkeypatch, draft_payload, mteam_payload, client,
                                  "1224888142", "Sigma Fantasy", date=1787612400000)
        assert body["league"]["has_history"] is False
        rows = client.post("/recommend", json={"session_id": sid, "current_pick": 1,
                                               "next_pick": 12, "topn": 50}).json()["results"]
        qb = next(r for r in rows if r["position"] == "QB")
        # The columns still exist - a league with no history must not return
        # differently-shaped rows - but they carry null rather than zero. The
        # board prints this number, and zero would claim this league drafts
        # everyone exactly at market, which was never measured.
        assert "bias_shift" in qb and qb["bias_shift"] is None
        assert qb["bias_reason"] is None
        # Scoring is unaffected either way: the pick estimate stays at ADP.
        assert qb["league_pick_est"] == qb["adp"]

    def test_scheduled_and_unscheduled_drafts_are_distinguishable(
            self, monkeypatch, draft_payload, mteam_payload, client):
        _, mcfl = self._connect(monkeypatch, draft_payload, mteam_payload, client,
                                  "780575", "McFL")
        assert mcfl["league"]["scheduled"] is False
        assert mcfl["league"]["draft_at"] is None

        _, sigma = self._connect(monkeypatch, draft_payload, mteam_payload, client,
                                  "1224888142", "Sigma Fantasy", date=1787612400000)
        assert sigma["league"]["scheduled"] is True
        assert sigma["league"]["draft_at"] == 1787612400000
        assert sigma["league"]["name"] == "Sigma Fantasy"

    def test_roster_need_is_adopted_from_the_league(
            self, monkeypatch, draft_payload, mteam_payload, client):
        _, body = self._connect(monkeypatch, draft_payload, mteam_payload, client,
                                  "780575", "McFL")
        assert set(body["roster_state"]) == {"QB", "RB", "WR", "TE", "FLEX", "K", "DST"}
        assert body["roster_state"]["RB"]["need"] == 2
