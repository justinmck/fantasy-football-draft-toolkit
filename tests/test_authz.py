"""One user cannot reach another user's anything.

Before this, the app had a single tenant baked in at three levels:

  1. A request with no token was served the *operator's* ESPN credentials, so
     an anonymous stranger browsed as whoever deployed the app.
  2. Session ids were `uuid4().hex[:8]` - 32 bits - and carried no owner, so
     anyone holding or guessing one could read the board, inject picks, spend
     the owner's ESPN rate limit, or disconnect them mid-draft.
  3. `league_id` was a free parameter on every analysis endpoint, so any league
     could be read by anyone who knew a small integer.

These pin all three shut. They are written as "user B tries something and gets
404", because 404 is the correct answer: 403 would confirm the thing exists.
"""
import pytest
from starlette.testclient import TestClient

import src.api as api
import src.espn_draft as espn_draft
import src.leagues as leagues_cache
from src.api import app
from src.state import SESSIONS

LEAGUE_A = "111111"
LEAGUE_B = "222222"


@pytest.fixture(autouse=True)
def _reachability(monkeypatch):
    """Each user can see exactly one league, and they are different ones."""
    def fake_list(creds, year):
        lid = LEAGUE_A if "AAAA" in creds.swid else LEAGUE_B
        return [espn_draft.LeagueRef(league_id=lid, name=f"L{lid}", season=year)]
    monkeypatch.setattr(leagues_cache, "list_leagues", fake_list)
    leagues_cache.clear()
    yield
    leagues_cache.clear()


def _session(client) -> str:
    return client.post("/session", json={"teams": 14, "rounds": 16}).json()["session_id"]


class TestNoIdentityMeansNoAccess:
    """The headline: no token must never resolve to the operator's account."""

    ENDPOINTS = [
        ("get", "/espn/leagues", None),
        ("post", "/espn/connect", {"session_id": "x"}),
        ("post", "/analysis/run", {"league_id": LEAGUE_A}),
        ("get", "/analysis?league_id=" + LEAGUE_A, None),
        ("get", "/analysis/status?league_id=" + LEAGUE_A, None),
        ("post", "/recommend", {"session_id": "x", "current_pick": 1, "next_pick": 2}),
        ("post", "/pick", {"session_id": "x", "player_id": 1, "position": "WR"}),
    ]

    @pytest.mark.parametrize("method,path,body", ENDPOINTS,
                             ids=[f"{m}:{p.split('?')[0]}" for m, p, _ in ENDPOINTS])
    def test_401_and_espn_is_never_contacted(self, monkeypatch, method, path, body):
        def must_not_be_called(*a, **k):     # pragma: no cover - the point is it isn't
            raise AssertionError("ESPN was contacted for an unauthenticated request")
        monkeypatch.setattr(api, "list_leagues", must_not_be_called)
        monkeypatch.setattr(api, "EspnDraftClient", must_not_be_called)

        anon = TestClient(app)
        res = getattr(anon, method)(path, **({"json": body} if body else {}))
        assert res.status_code == 401, res.text
        assert res.headers.get("X-Auth-Code") == "no_token"

    def test_an_unknown_token_is_signed_out_not_the_operator(self):
        bogus = TestClient(app, headers={"X-Device-Token": "not-a-token-we-issued"})
        res = bogus.get("/espn/leagues")
        assert res.status_code == 401
        # A distinct code, because the UI must react differently: "signed out"
        # keeps an in-progress draft on screen, "no token" sends you to sign-in.
        assert res.headers.get("X-Auth-Code") == "auth_expired"
        assert "leagues" not in res.json()


class TestSessionsHaveOwners:
    def test_ids_are_a_full_uuid4(self, guest):
        sid = _session(guest)
        assert len(sid) == 32, "8 hex chars is 32 bits and enumerable"

    def test_b_cannot_read_as_board(self, user_a, user_b):
        sid = _session(user_a)
        res = user_b.post("/recommend", json={
            "session_id": sid, "current_pick": 1, "next_pick": 15})
        assert res.status_code == 404

    def test_b_cannot_inject_a_pick(self, user_a, user_b):
        """`pick` is deliberately not idempotent, so an injected pick would
        double-count a roster slot and poison every later recommendation."""
        sid = _session(user_a)
        res = user_b.post("/pick", json={
            "session_id": sid, "player_id": 2, "position": "RB", "is_my_pick": True})
        assert res.status_code == 404
        # And nothing was applied on the way to the 404.
        assert SESSIONS[sid].draft_log == []
        assert all(v["have"] == 0 for v in SESSIONS[sid].roster_state.values())

    def test_b_cannot_poll_espn_on_as_credentials(self, user_a, user_b):
        sid = _session(user_a)
        assert user_b.get(f"/espn/sync/{sid}").status_code == 404

    def test_b_cannot_disconnect_a(self, user_a, user_b):
        """This endpoint used to check nothing at all."""
        sid = _session(user_a)
        assert user_b.post("/espn/disconnect", json={"session_id": sid}).status_code == 404

    def test_a_can_still_use_their_own_session(self, user_a):
        """The checks must not lock the owner out of their own board."""
        sid = _session(user_a)
        assert user_a.post("/pick", json={
            "session_id": sid, "player_id": 2, "position": "RB",
            "is_my_pick": True}).status_code == 200

    def test_re_signing_in_keeps_the_session(self, user_a, user_a_again):
        """The reason ownership is keyed on the account, not the device token.

        Tokens expire and rotate. A drafter who signs in again halfway through
        must still own the board they are in the middle of - keying on the
        token would orphan it at the worst possible moment.
        """
        sid = _session(user_a)
        assert user_a_again.post("/recommend", json={
            "session_id": sid, "current_pick": 1, "next_pick": 15}).status_code == 200


class TestLeagueAccess:
    def test_a_league_you_cannot_reach_is_404(self, user_a):
        for path in (f"/analysis?league_id={LEAGUE_B}",
                     f"/analysis/status?league_id={LEAGUE_B}"):
            assert user_a.get(path).status_code == 404, path
        assert user_a.post("/analysis/run",
                           json={"league_id": LEAGUE_B}).status_code == 404

    def test_your_own_league_is_allowed(self, user_a):
        assert user_a.get(f"/analysis/status?league_id={LEAGUE_A}").status_code == 200

    def test_reachability_is_cached(self, monkeypatch, user_a):
        """One fan-endpoint call per user per TTL, not one per request."""
        calls = []

        def counted(creds, year):
            calls.append(1)
            return [espn_draft.LeagueRef(league_id=LEAGUE_A, name="A", season=year)]
        monkeypatch.setattr(leagues_cache, "list_leagues", counted)
        leagues_cache.clear()

        for _ in range(4):
            user_a.get(f"/analysis/status?league_id={LEAGUE_A}")
        assert len(calls) == 1

    def test_a_stale_list_is_served_when_espn_blips(self, monkeypatch, user_a):
        """Locking a drafter out of their own league because ESPN hiccuped is
        far worse than answering from a list a few minutes old."""
        assert user_a.get(f"/analysis/status?league_id={LEAGUE_A}").status_code == 200

        def down(creds, year):
            raise espn_draft.EspnUnavailable("Could not reach ESPN.")
        monkeypatch.setattr(leagues_cache, "list_leagues", down)
        monkeypatch.setattr(leagues_cache, "TTL", -1.0)   # force a refresh attempt

        assert user_a.get(f"/analysis/status?league_id={LEAGUE_A}").status_code == 200


class TestGuests:
    def test_a_guest_can_draft_by_hand(self, guest):
        """Clicking picks in must never require handing over ESPN cookies."""
        sid = _session(guest)
        assert guest.post("/pick", json={
            "session_id": sid, "player_id": 2, "position": "RB",
            "is_my_pick": True}).status_code == 200

    def test_a_guest_cannot_reach_espn(self, guest):
        res = guest.get("/espn/leagues")
        assert res.status_code == 403
        assert res.headers.get("X-Auth-Code") == "needs_espn"

    def test_session_without_a_token_mints_one(self):
        anon = TestClient(app)
        body = anon.post("/session", json={"teams": 14}).json()
        assert body["token"], "a caller with no identity must be given one"
        # And that token owns the session it just created.
        owned = TestClient(app, headers={"X-Device-Token": body["token"]})
        assert owned.post("/pick", json={
            "session_id": body["session_id"], "player_id": 2,
            "position": "RB"}).status_code == 200


class TestJobs:
    def test_another_users_job_is_404(self, monkeypatch, user_a, user_b):
        from src.jobs import start
        job = start("history", LEAGUE_A, lambda report: {"ok": True},
                    owner="somebody-else")
        assert user_b.get(f"/analysis/job/{job.id}").status_code == 404

    def test_the_owner_can_read_their_job(self, user_a, user_a_id):
        from src.jobs import start

        job = start("history", LEAGUE_A, lambda report: {"ok": True},
                    owner=user_a_id)
        assert user_a.get(f"/analysis/job/{job.id}").status_code == 200
