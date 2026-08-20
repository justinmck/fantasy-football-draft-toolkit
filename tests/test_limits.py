"""Rate limiting, and the one property that actually matters.

There was none anywhere in the app. `/auth/connect` is the sharp case: every
attempt makes an outbound request to ESPN carrying attacker-supplied cookies,
so an open endpoint is both a credential oracle and a way to get the
deployment's address throttled by ESPN.

So the headline test is not "returns 429" - it is that the limiter runs
*before* the outbound call, and that the ESPN stub therefore stops being
called. A limiter that 429s after doing the work protects nobody.
"""
import pytest
from starlette.testclient import TestClient

import src.auth as auth
from src import authdb, limits
from src.api import app
from src.espn_draft import LeagueRef

client = TestClient(app)
SWID = "{LIMIT-1111-2222-3333-444444444444}"
S2 = "s2-for-limit-tests"


@pytest.fixture(autouse=True)
def clean():
    with authdb.engine().begin() as conn:
        conn.exec_driver_sql("DELETE FROM device_tokens")
    limits.reset()
    yield
    limits.reset()


@pytest.fixture
def espn_calls(monkeypatch):
    """Counts outbound ESPN calls, so we can prove they stopped."""
    calls = []

    def fake(creds, year):
        calls.append(1)
        return [LeagueRef(league_id="780575", name="McFL", season=year)]

    monkeypatch.setattr(auth, "list_leagues", fake)
    monkeypatch.setattr(auth, "EspnDraftClient", lambda *a, **k: _Settings())
    return calls


class _Settings:
    def settings(self):
        return None


class TestAuthConnect:
    def test_the_burst_is_allowed_then_refused(self, espn_calls):
        codes = [client.post("/auth/connect",
                             json={"swid": SWID, "espn_s2": S2}).status_code
                 for _ in range(7)]
        assert codes[:5] == [200] * 5, codes
        assert codes[5:] == [429, 429], codes

    def test_the_limiter_runs_before_the_outbound_espn_call(self, espn_calls):
        """The whole point. A 429 issued *after* calling ESPN protects nothing -
        the attacker still gets their cookies checked and we still get the
        outbound traffic."""
        for _ in range(9):
            client.post("/auth/connect", json={"swid": SWID, "espn_s2": S2})
        assert len(espn_calls) == 5, (
            f"ESPN was called {len(espn_calls)} times for 9 requests; "
            "the limiter is running after the call, not before")

    def test_a_429_carries_retry_after(self, espn_calls):
        for _ in range(6):
            res = client.post("/auth/connect", json={"swid": SWID, "espn_s2": S2})
        assert res.status_code == 429
        assert int(res.headers["Retry-After"]) > 0

    def test_the_bucket_refills(self, espn_calls, monkeypatch):
        t = [1000.0]
        monkeypatch.setattr(limits, "now", lambda: t[0])
        limits.reset()
        for _ in range(5):
            client.post("/auth/connect", json={"swid": SWID, "espn_s2": S2})
        assert client.post("/auth/connect",
                           json={"swid": SWID, "espn_s2": S2}).status_code == 429
        # One token per 180s.
        t[0] += 200
        assert client.post("/auth/connect",
                           json={"swid": SWID, "espn_s2": S2}).status_code == 200


class TestBuckets:
    def test_limits_are_per_key(self):
        """One noisy address must not spend everyone else's budget."""
        for _ in range(5):
            limits.check("scope", "a", rate_per_sec=1 / 180, burst=5)
        with pytest.raises(Exception):
            limits.check("scope", "a", rate_per_sec=1 / 180, burst=5)
        limits.check("scope", "b", rate_per_sec=1 / 180, burst=5)  # unaffected

    def test_scopes_are_independent(self):
        for _ in range(5):
            limits.check("one", "k", rate_per_sec=1 / 180, burst=5)
        limits.check("two", "k", rate_per_sec=1 / 180, burst=5)


class TestForwardedFor:
    """X-Forwarded-For is attacker-controlled unless a proxy is guaranteed.

    Trusting it unconditionally would make every limit here bypassable by
    setting one header, which is worse than no limiter: it looks protected.
    """

    def _req(self, headers):
        class R:
            def __init__(self, h):
                self.headers = h
                self.client = type("C", (), {"host": "10.0.0.1"})()
        return R(headers)

    def test_ignored_by_default(self, monkeypatch):
        monkeypatch.delenv("TRUSTED_PROXY", raising=False)
        assert limits.client_ip(self._req({"x-forwarded-for": "1.2.3.4"})) == "10.0.0.1"

    def test_used_when_a_proxy_is_declared(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXY", "1")
        assert limits.client_ip(self._req({"x-forwarded-for": "1.2.3.4"})) == "1.2.3.4"

    def test_the_hop_is_counted_from_the_right(self, monkeypatch):
        """Everything left of the trusted hop is attacker-supplied."""
        monkeypatch.setenv("TRUSTED_PROXY", "1")
        got = limits.client_ip(self._req({"x-forwarded-for": "9.9.9.9, 1.2.3.4"}))
        assert got == "1.2.3.4"


class TestFieldBounds:
    """Unbounded fields on a public endpoint are a memory-allocation primitive."""

    @pytest.mark.parametrize("body", [
        {"teams": 5000, "rounds": 16},
        {"teams": 14, "rounds": 100000},
    ])
    def test_absurd_session_shapes_are_rejected(self, body):
        assert client.post("/session", json=body).status_code == 422

    def test_a_giant_cookie_is_rejected_before_it_is_stored(self):
        res = client.post("/auth/connect",
                          json={"swid": SWID, "espn_s2": "x" * 100_000})
        assert res.status_code == 422
        assert authdb.count() == 0

    def test_a_non_numeric_league_id_is_rejected(self):
        tok = auth.issue(SWID, S2)
        res = client.post("/analysis/run", json={"league_id": "../etc/passwd"},
                          headers={"X-Device-Token": tok})
        assert res.status_code == 422
