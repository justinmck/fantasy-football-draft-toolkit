"""Sign-in, and the rule that credentials never leave the server.

The whole point of the token indirection is that the browser holds an opaque
string rather than ESPN session cookies. These pin that, plus the failure
modes that would otherwise send someone debugging the wrong thing.
"""
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import src.api as api
import src.auth as auth
from src import authdb, limits
from src.api import app
from src.espn_draft import (
    AUTH_MESSAGE,
    S2_MESSAGE,
    EspnAuthError,
    EspnCredentials,
    EspnUnavailable,
    LeagueRef,
)

client = TestClient(app)


@pytest.fixture(autouse=True)
def empty_credential_store():
    """Each test starts with no tokens and a full rate-limit budget.

    The store is a real SQLite file now rather than a dict, so it persists
    across tests within a run; several of these assert on counts. The limiter
    is likewise process-global, and these tests post to /auth/connect far more
    often than any human would - without a reset they trip it and every
    assertion afterwards sees a 429.
    """
    with authdb.engine().begin() as conn:
        conn.exec_driver_sql("DELETE FROM device_tokens")
    limits.reset()
    yield
SWID = "{SENTINEL-1111-2222-3333-444444444444}"
S2 = "S2-SENTINEL-do-not-leak"


class _FakeClient:
    """Stands in for the settings read that proves espn_s2 works."""
    raises = None

    def __init__(self, creds, year, league_id=None):
        self.creds = creds

    def settings(self):
        if _FakeClient.raises:
            raise _FakeClient.raises
        return object()


@pytest.fixture
def espn_ok(monkeypatch):
    _FakeClient.raises = None
    monkeypatch.setattr(auth, "list_leagues", lambda creds, year: [
        LeagueRef(league_id="780575", name="McFL", season=year),
        LeagueRef(league_id="197229335", name="Sigmas", season=year),
    ])
    monkeypatch.setattr(auth, "EspnDraftClient", _FakeClient)


class TestConnect:
    def test_returns_a_token_and_the_leagues(self, espn_ok):
        r = client.post("/auth/connect", json={"swid": SWID, "espn_s2": S2})
        assert r.status_code == 200
        body = r.json()
        assert body["token"] and len(body["token"]) > 20
        assert [l["name"] for l in body["leagues"]] == ["McFL", "Sigmas"]

    def test_never_returns_the_credentials(self, espn_ok):
        """The reason the token exists at all."""
        text = client.post("/auth/connect", json={"swid": SWID, "espn_s2": S2}).text
        assert SWID not in text and S2 not in text

    def test_missing_fields_are_rejected_before_calling_espn(self):
        assert client.post("/auth/connect", json={"swid": "", "espn_s2": S2}).status_code == 400
        assert client.post("/auth/connect", json={"swid": SWID, "espn_s2": " "}).status_code == 400

    def test_bad_credentials_say_so_rather_than_blaming_the_network(self, monkeypatch):
        """A 404 from the fan endpoint means a bad SWID - it's in the path.

        Reporting it as "could not reach ESPN" sends someone to debug their
        wifi instead of re-copying their cookies.
        """
        def boom(creds, year):
            raise EspnAuthError(AUTH_MESSAGE)
        monkeypatch.setattr(auth, "list_leagues", boom)
        r = client.post("/auth/connect", json={"swid": SWID, "espn_s2": S2})
        assert r.status_code == 502 and r.json()["detail"] == AUTH_MESSAGE

    def test_unreachable_is_503(self, monkeypatch):
        def boom(creds, year):
            raise EspnUnavailable("Could not reach ESPN.")
        monkeypatch.setattr(auth, "list_leagues", boom)
        assert client.post("/auth/connect", json={"swid": SWID, "espn_s2": S2}).status_code == 503

    def test_a_good_swid_with_a_bad_s2_is_rejected_at_sign_in(self, espn_ok):
        """The failure that looked like three unscheduled leagues.

        The fan endpoint carries the SWID in its path and is satisfied by it
        alone, so entering the SWID in both boxes passed sign-in, listed every
        league by name, and then failed every per-league read. Sign-in now
        proves the second cookie too.
        """
        _FakeClient.raises = EspnAuthError(AUTH_MESSAGE)
        r = client.post("/auth/connect", json={"swid": SWID, "espn_s2": SWID})
        assert r.status_code == 502
        assert r.json()["detail"] == S2_MESSAGE
        assert "espn_s2" in r.json()["detail"]   # names the cookie to fix

    def test_a_rejected_pair_is_not_stored(self, espn_ok):
        _FakeClient.raises = EspnAuthError(AUTH_MESSAGE)
        client.post("/auth/connect", json={"swid": SWID, "espn_s2": SWID})
        assert authdb.count() == 0

    def test_an_unreachable_settings_call_is_not_blamed_on_the_cookie(self, espn_ok):
        """ESPN being down is not the user's cookie being wrong."""
        _FakeClient.raises = EspnUnavailable("Could not reach ESPN.")
        assert client.post("/auth/connect",
                           json={"swid": SWID, "espn_s2": S2}).status_code == 503

    def test_valid_cookies_with_no_leagues_is_explained(self, monkeypatch):
        monkeypatch.setattr(auth, "list_leagues", lambda creds, year: [])
        r = client.post("/auth/connect", json={"swid": SWID, "espn_s2": S2})
        assert r.status_code == 502 and "no football leagues" in r.json()["detail"]


class TestSession:
    def test_a_valid_token_is_signed_in(self, espn_ok):
        tok = client.post("/auth/connect", json={"swid": SWID, "espn_s2": S2}).json()["token"]
        r = client.get("/auth/session", headers={"X-Device-Token": tok})
        assert r.status_code == 200 and r.json()["signed_in"] is True

    @pytest.mark.parametrize("headers", [{}, {"X-Device-Token": "not-a-real-token"}])
    def test_no_or_unknown_token_is_401(self, headers):
        assert client.get("/auth/session", headers=headers).status_code == 401

    def test_forget_signs_the_device_out(self, espn_ok):
        tok = client.post("/auth/connect", json={"swid": SWID, "espn_s2": S2}).json()["token"]
        assert client.post("/auth/forget", headers={"X-Device-Token": tok}).json()["forgotten"] is True
        assert client.get("/auth/session", headers={"X-Device-Token": tok}).status_code == 401

    def test_forgetting_an_unknown_token_is_harmless(self):
        r = client.post("/auth/forget", headers={"X-Device-Token": "nope"})
        assert r.status_code == 200 and r.json()["forgotten"] is False


class TestTokenStore:
    def test_file_is_owner_only(self, espn_ok):
        """It holds session cookies; it is a credential store."""
        client.post("/auth/connect", json={"swid": SWID, "espn_s2": S2})
        path = Path(str(authdb.engine().url)[len("sqlite:///"):])
        assert oct(path.stat().st_mode)[-3:] == "600"

    def test_the_bearer_token_is_not_stored(self):
        """A stolen copy of this database must not yield working tokens."""
        tok = authdb.insert(user_id="u", kind="espn", swid=SWID, espn_s2=S2)
        with authdb.engine().begin() as conn:
            blob = " ".join(str(r) for r in conn.exec_driver_sql(
                "SELECT * FROM device_tokens").fetchall())
        assert tok not in blob
        assert authdb.hash_token(tok) in blob

    def test_the_cookies_are_not_stored_in_the_clear(self):
        """The whole point of encrypting at rest: a file that leaves the
        machine - a commit, a backup, an scp of data/ - carries no cookies."""
        authdb.insert(user_id="u", kind="espn", swid=SWID, espn_s2=S2)
        raw = Path(str(authdb.engine().url)[len("sqlite:///"):]).read_bytes()
        assert SWID.encode() not in raw
        assert S2.encode() not in raw

    def test_an_expired_token_stops_working(self):
        tok = authdb.insert(user_id="u", kind="espn", swid=SWID, espn_s2=S2)
        assert authdb.lookup(tok) is not None
        with authdb.engine().begin() as conn:
            conn.exec_driver_sql(
                "UPDATE device_tokens SET expires = 1 WHERE token_hash = "
                f"'{authdb.hash_token(tok)}'")
        assert authdb.lookup(tok) is None

    def test_an_idle_token_stops_working(self):
        """Absolute expiry alone would let a stolen token live for a month."""
        tok = authdb.insert(user_id="u", kind="espn", swid=SWID, espn_s2=S2)
        with authdb.engine().begin() as conn:
            conn.exec_driver_sql(
                "UPDATE device_tokens SET last_used = 1 WHERE token_hash = "
                f"'{authdb.hash_token(tok)}'")
        assert authdb.lookup(tok) is None

    def test_a_row_encrypted_under_a_lost_key_is_treated_as_signed_out(self, monkeypatch):
        """Never a 500 mid-draft: an unreadable row is the same event as an
        expired one, and the user is asked to sign in again."""
        tok = authdb.insert(user_id="u", kind="espn", swid=SWID, espn_s2=S2)
        monkeypatch.setattr(authdb, "unseal", lambda blob: None)
        assert authdb.lookup(tok) is None

    def test_credentials_round_trip_but_only_via_the_token(self, espn_ok):
        tok = client.post("/auth/connect", json={"swid": SWID, "espn_s2": S2}).json()["token"]
        creds = auth.credentials_for(tok)
        assert creds.swid == SWID and creds.espn_s2 == S2
        assert auth.credentials_for("wrong") is None
        assert auth.credentials_for(None) is None

    def test_tokens_are_unique_per_signin(self, espn_ok):
        a = client.post("/auth/connect", json={"swid": SWID, "espn_s2": S2}).json()["token"]
        b = client.post("/auth/connect", json={"swid": SWID, "espn_s2": S2}).json()["token"]
        assert a != b

    def test_a_corrupt_store_does_not_crash_sign_in(self, espn_ok):
        Path(auth.TOKEN_FILE).parent.mkdir(parents=True, exist_ok=True)
        Path(auth.TOKEN_FILE).write_text("{ not json")
        assert auth.credentials_for("anything") is None
        assert client.post("/auth/connect", json={"swid": SWID, "espn_s2": S2}).status_code == 200


class TestNoFallbackToTheOperator:
    """The replacement for what used to be `TestFallback`.

    That test asserted `api._creds_for(None).swid == "env-swid"` - that a
    request with no token was served the *operator's* ESPN credentials from
    `.env`. It encoded the vulnerability as the contract. On one laptop it was
    merely untidy; the moment this app is reachable from the internet it means
    every stranger is browsing as the person who deployed it.
    """

    def test_the_resolver_is_gone_entirely(self):
        """Not renamed or guarded - removed, so it cannot be called by accident."""
        assert not hasattr(api, "_creds_for")

    def test_api_never_imports_operator_credentials(self):
        """A structural guard: the symbol must not be reachable from a request.

        `load_credentials` still exists for the notebooks and CLI scripts, which
        legitimately run *as* the operator. It must simply never be in scope in
        the request path, where re-adding a fallback would be a one-line edit.
        """
        source = Path(api.__file__).read_text()
        assert "load_credentials" not in source

    @pytest.mark.parametrize("headers", [
        pytest.param({}, id="no-token"),
        pytest.param({"X-Device-Token": "not-a-real-token"}, id="bogus-token"),
    ])
    def test_espn_endpoints_401_rather_than_acting_as_the_operator(self, monkeypatch, headers):
        def must_not_be_called(*a, **k):     # pragma: no cover - the point is it isn't
            raise AssertionError("ESPN was contacted without an identity")
        monkeypatch.setattr(api, "list_leagues", must_not_be_called)

        res = client.get("/espn/leagues", headers=headers)
        assert res.status_code == 401
        # And critically: no league names in the body.
        assert "leagues" not in res.json()
