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
from src.api import app
from src.espn_draft import AUTH_MESSAGE, EspnAuthError, EspnCredentials, EspnUnavailable, LeagueRef

client = TestClient(app)
SWID = "{SENTINEL-1111-2222-3333-444444444444}"
S2 = "S2-SENTINEL-do-not-leak"


@pytest.fixture(autouse=True)
def isolated_token_file(tmp_path, monkeypatch):
    """Never touch the real credential store from a test."""
    monkeypatch.setattr(auth, "TOKEN_FILE", tmp_path / "device_tokens.json")


@pytest.fixture
def espn_ok(monkeypatch):
    monkeypatch.setattr(auth, "list_leagues", lambda creds, year: [
        LeagueRef(league_id="780575", name="McFL", season=year),
        LeagueRef(league_id="197229335", name="Sigmas", season=year),
    ])


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
        assert oct(Path(auth.TOKEN_FILE).stat().st_mode)[-3:] == "600"

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


class TestFallback:
    def test_env_credentials_still_work_with_no_token(self, monkeypatch):
        """A checkout that has never signed in must keep working."""
        monkeypatch.setattr(api, "load_credentials",
                            lambda: EspnCredentials("780575", "env-swid", "env-s2"))
        assert api._creds_for(None).swid == "env-swid"
        assert api._creds_for("bogus-token").swid == "env-swid"
