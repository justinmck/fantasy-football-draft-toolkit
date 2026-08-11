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

client = TestClient(app)
FIXTURES = Path(__file__).parent / "fixtures"
MY_TEAM = 8

SWID_SENTINEL = "{SWID-SENTINEL-0000-0000-000000000000}"
S2_SENTINEL = "S2-SENTINEL-abcdef0123456789"


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
def connected(monkeypatch, draft_payload, mteam_payload):
    """A session wired to a fake ESPN, with the throttle disabled by default."""
    fake = FakeEspn(draft_payload, mteam_payload)

    monkeypatch.setattr(
        api, "load_credentials",
        lambda: EspnCredentials("123", SWID_SENTINEL, S2_SENTINEL),
    )
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

    def test_unknown_session_is_404(self, monkeypatch):
        monkeypatch.setattr(
            api, "load_credentials",
            lambda: EspnCredentials("123", SWID_SENTINEL, S2_SENTINEL),
        )
        assert client.post("/espn/connect", json={"session_id": "nope"}).status_code == 404

    def test_auth_failure_is_502_with_a_constant_message(self, monkeypatch):
        def boom():
            raise EspnAuthError(AUTH_MESSAGE)
        monkeypatch.setattr(api, "load_credentials", boom)
        sid = client.post("/session", json={}).json()["session_id"]
        res = client.post("/espn/connect", json={"session_id": sid})
        assert res.status_code == 502
        assert res.json()["detail"] == AUTH_MESSAGE

    def test_unreachable_is_503(self, monkeypatch):
        def boom():
            raise EspnUnavailable("Could not reach ESPN.")
        monkeypatch.setattr(api, "load_credentials", boom)
        sid = client.post("/session", json={}).json()["session_id"]
        assert client.post("/espn/connect", json={"session_id": sid}).status_code == 503


class TestSync:
    def test_picks_apply_without_anyone_clicking(self, connected):
        sid, fake, _ = connected
        fake.upto = 3
        body = client.get(f"/espn/sync/{sid}").json()
        assert [p["overall_pick"] for p in body["new_picks"]] == [1, 2, 3]
        assert body["drafted_count"] == 3
        assert body["current_pick"] == 4

    def test_my_own_picks_fill_my_roster(self, connected):
        sid, fake, _ = connected
        fake.upto = 4  # team 8 drafted 4th in 2025
        body = client.get(f"/espn/sync/{sid}").json()
        mine = [p for p in body["new_picks"] if p["is_me"]]
        assert len(mine) == 1 and mine[0]["overall_pick"] == 4
        assert mine[0]["filled_slot"] is not None
        assert sum(v["have"] for v in body["roster_state"].values()) == 1

    def test_other_teams_picks_leave_the_pool_without_touching_my_roster(self, connected):
        sid, fake, _ = connected
        fake.upto = 3  # none of these are team 8's
        body = client.get(f"/espn/sync/{sid}").json()
        assert all(p["is_me"] is False for p in body["new_picks"])
        assert body["drafted_count"] == 3
        assert all(v["have"] == 0 for v in body["roster_state"].values())

    def test_version_moves_only_when_something_happened(self, connected):
        sid, fake, _ = connected
        fake.upto = 5
        first = client.get(f"/espn/sync/{sid}").json()
        second = client.get(f"/espn/sync/{sid}").json()
        assert second["version"] == first["version"]
        assert second["new_picks"] == []
        fake.upto = 6
        third = client.get(f"/espn/sync/{sid}").json()
        assert third["version"] == first["version"] + 1

    def test_full_draft_reports_complete(self, connected):
        sid, fake, _ = connected
        fake.upto = 224
        body = client.get(f"/espn/sync/{sid}").json()
        assert body["complete"] is True
        assert body["current_pick"] is None
        assert body["drafted_count"] == 224
        assert body["bench_filled"] == 7

    def test_auth_failure_keeps_the_board_alive(self, connected):
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

    def test_network_failure_serves_the_last_good_state(self, connected):
        sid, fake, _ = connected
        fake.upto = 7
        client.get(f"/espn/sync/{sid}")
        fake.raise_on_draft = EspnUnavailable("Could not reach ESPN.")
        body = client.get(f"/espn/sync/{sid}").json()
        assert body["status"] == "stale"
        assert body["drafted_count"] == 7

    def test_throttle_collapses_rapid_polls(self, monkeypatch, connected):
        """StrictMode double-mounts and a second tab both multiply the poll
        rate; without the throttle that lands on ESPN."""
        sid, fake, _ = connected
        monkeypatch.setattr(api, "_MIN_POLL_SECONDS", 60.0)
        before = fake.calls
        for _ in range(4):
            client.get(f"/espn/sync/{sid}")
        assert fake.calls == before  # every one served from the last snapshot

    def test_sync_without_connect_is_404(self):
        sid = client.post("/session", json={}).json()["session_id"]
        assert client.get(f"/espn/sync/{sid}").status_code == 404


class TestDisconnect:
    def test_keeps_the_session_and_its_picks(self, connected):
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

    def test_disconnecting_when_never_connected_is_harmless(self):
        sid = client.post("/session", json={}).json()["session_id"]
        assert client.post("/espn/disconnect", json={"session_id": sid}).status_code == 200


class TestNoCredentialLeak:
    def test_no_response_body_contains_a_credential(self, connected):
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

    def test_sync_payload_is_json_safe(self, connected):
        """None must serialise as null - position, player_name and filled_slot
        are all nullable here, and NaN has 500ed this API before."""
        sid, fake, _ = connected
        fake.upto = 60
        text = client.get(f"/espn/sync/{sid}").text
        assert "NaN" not in text and "Infinity" not in text
