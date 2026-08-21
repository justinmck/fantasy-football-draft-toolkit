"""Reaching seasons ESPN has moved off the current endpoint.

A league's recent seasons live at `/seasons/{year}/segments/0/leagues/{id}`.
Older ones are served only from `/leagueHistory/{id}?seasonId={year}`, and the
modern path 404s for them. Asking only the modern path therefore reports a
league as having begun whenever ESPN last migrated it, which from the caller's
side is indistinguishable from the league not existing yet - the real league
this was found on has seasons before the earliest one the pull could see.

The two endpoints also disagree about shape: `leagueHistory` answers with a
one-element list where the other answers with an object.
"""
import pytest

from src.espn_draft import EspnAuthError, EspnCredentials, EspnUnavailable
from src import espn_history

CREDS = EspnCredentials(swid="{AAAAAAAA-1111-2222-3333-444444444444}", espn_s2="x",
                        league_id="780575")
LEAGUE = "780575"


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture
def calls(monkeypatch):
    """Record every outbound URL, and reply from a per-URL script."""
    seen = []
    script = {}

    def fake_fetch(creds, url, params, headers):
        seen.append((url, dict(params or {})))
        for fragment, response in script.items():
            if fragment in url:
                return response
        return FakeResponse(404)

    monkeypatch.setattr(espn_history, "_fetch", fake_fetch)
    return seen, script


class TestHistoricalFallback:
    def test_a_modern_season_costs_one_request(self, calls):
        seen, script = calls
        script["/seasons/2025/"] = FakeResponse(200, {"teams": [{"id": 1}]})
        out = espn_history._get(CREDS, LEAGUE, 2025, "mTeam")
        assert out == {"teams": [{"id": 1}]}
        assert len(seen) == 1, "a season that works must not also hit the fallback"

    def test_a_migrated_season_falls_back_and_is_unwrapped(self, calls):
        seen, script = calls
        script["/seasons/2016/"] = FakeResponse(404)
        script["/leagueHistory/"] = FakeResponse(200, [{"teams": [{"id": 7}]}])
        out = espn_history._get(CREDS, LEAGUE, 2016, "mTeam")
        assert out == {"teams": [{"id": 7}]}, "the list wrapper must be unwrapped"
        assert len(seen) == 2
        assert seen[1][1]["seasonId"] == 2016, "leagueHistory selects the season by param"

    def test_a_season_missing_from_both_is_not_an_error(self, calls):
        seen, script = calls
        script["/seasons/2011/"] = FakeResponse(404)
        script["/leagueHistory/"] = FakeResponse(404)
        assert espn_history._get(CREDS, LEAGUE, 2011, "mTeam") == {}

    def test_an_empty_history_list_is_not_an_index_error(self, calls):
        _, script = calls
        script["/seasons/2011/"] = FakeResponse(404)
        script["/leagueHistory/"] = FakeResponse(200, [])
        assert espn_history._get(CREDS, LEAGUE, 2011, "mTeam") == {}

    def test_bad_cookies_do_not_trigger_a_second_doomed_request(self, calls):
        seen, script = calls
        script["/seasons/2016/"] = FakeResponse(401)
        with pytest.raises(EspnAuthError):
            espn_history._get(CREDS, LEAGUE, 2016, "mTeam")
        assert len(seen) == 1, "no point asking the fallback with rejected cookies"

    def test_auth_failure_on_the_fallback_still_raises(self, calls):
        _, script = calls
        script["/seasons/2016/"] = FakeResponse(404)
        script["/leagueHistory/"] = FakeResponse(403)
        with pytest.raises(EspnAuthError):
            espn_history._get(CREDS, LEAGUE, 2016, "mTeam")

    def test_a_server_error_is_unavailable_not_a_missing_season(self, calls):
        _, script = calls
        script["/seasons/2016/"] = FakeResponse(500)
        with pytest.raises(EspnUnavailable):
            espn_history._get(CREDS, LEAGUE, 2016, "mTeam")


class TestAvailableSeasons:
    def test_seasons_found_only_via_history_are_reported(self, calls):
        """The whole point: a league whose early years have been migrated must
        not be reported as having started later than it did."""
        _, script = calls
        script["/seasons/"] = FakeResponse(404)
        script["/leagueHistory/"] = FakeResponse(200, [{"teams": [{"id": 1}]}])
        found = espn_history.available_seasons(CREDS, LEAGUE, range(2016, 2019))
        assert found == [2016, 2017, 2018]

    def test_a_year_with_no_teams_either_way_is_skipped(self, calls):
        _, script = calls
        script["/seasons/"] = FakeResponse(404)
        script["/leagueHistory/"] = FakeResponse(200, [{"teams": []}])
        assert espn_history.available_seasons(CREDS, LEAGUE, range(2016, 2019)) == []
