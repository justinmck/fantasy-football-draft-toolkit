"""Tests for the retrospective analysis behind the UI's Analysis tab.

The fixture database deliberately carries only the tables the *recommender*
needs, so these also pin the degradation path: a database where the draft
history hasn't been loaded must render an empty Analysis tab rather than
500ing the endpoint.
"""
import numpy as np
import pandas as pd
import pytest
from starlette.testclient import TestClient

from src.analysis import _pearson, league_analysis
from src.api import app
from src.db import engine

import src.auth as auth
import src.leagues as leagues_cache

# The analysis endpoints are now authenticated and league-scoped: `league_id`
# is required and checked against the leagues the caller's own credentials can
# reach. It used to be optional and unauthenticated, which let anyone read any
# league's managers, standings and per-person draft tendencies by guessing a
# small integer.
LEAGUE = "780575"


@pytest.fixture
def client(monkeypatch):
    import src.espn_draft as espn_draft
    monkeypatch.setattr(
        leagues_cache, "list_leagues",
        lambda creds, year: [espn_draft.LeagueRef(league_id=LEAGUE, name="McFL",
                                                  season=year)])
    leagues_cache.clear()
    token = auth.issue("{ANALYSIS-1111-2222-3333-444444444444}", "s2-analysis-" + "z" * 40)
    yield TestClient(app, headers={"X-Device-Token": token})
    leagues_cache.clear()


def test_analysis_endpoint_ok_without_draft_history(client):
    """The fixture DB has no `players`/`teams` tables - this must not raise."""
    res = client.get("/analysis", params={"league_id": LEAGUE})
    assert res.status_code == 200
    body = res.json()
    assert body["draft_performance"]["teams"] == []
    assert body["draft_performance"]["correlation"] is None
    assert body["steals_and_reaches"]["steals"] == []
    # The new league sections must degrade the same way rather than raising.
    assert body["career_performance"]["managers"] == []
    assert body["expectations"]["teams"] == []
    # `projection_value` was removed: the tab replaced it with
    # `projection_accuracy` and nothing had read it since.
    assert "projection_value" not in body


def test_analysis_still_returns_reliability_tables(client):
    """position_reliability exists in the fixture, so it should come through
    even though the draft-history half of the payload is empty."""
    body = client.get("/analysis", params={"league_id": LEAGUE}).json()
    assert len(body["position_reliability"]) > 0
    assert {"position", "r2", "reliability"} <= set(body["position_reliability"][0])


def test_analysis_accepts_year_override(client):
    res = client.get("/analysis", params={"league_id": LEAGUE, "year": 2021})
    assert res.status_code == 200
    assert res.json()["year"] == 2021


def test_analysis_cache_is_keyed_on_the_year(client):
    """The season switcher hits this endpoint once per year on the same
    database. A cache keyed only on the database's identity would serve the
    first year asked for to every subsequent request, and the page would look
    frozen while the chips moved."""
    first = client.get("/analysis", params={"league_id": LEAGUE, "year": 2021}).json()
    second = client.get("/analysis", params={"league_id": LEAGUE, "year": 2022}).json()
    again = client.get("/analysis", params={"league_id": LEAGUE, "year": 2021}).json()
    assert first["year"] == 2021
    assert second["year"] == 2022
    assert again["year"] == 2021


def test_analysis_payload_is_json_safe(client):
    """No NaN anywhere: it isn't valid JSON and would 500 the endpoint, which
    is exactly the bug the recommender already carries a regression test for."""
    raw = client.get("/analysis", params={"league_id": LEAGUE}).text
    assert "NaN" not in raw
    assert "Infinity" not in raw


class TestPearson:
    def test_returns_r_and_p_for_a_real_relationship(self):
        x = pd.Series([1.0, 2, 3, 4, 5])
        y = pd.Series([2.0, 4, 6, 8, 10])
        out = _pearson(x, y)
        assert out["r"] == pytest.approx(1.0)
        assert out["n"] == 5

    def test_negative_relationship_keeps_its_sign(self):
        # The draft-VORP/standings correlation is negative by construction
        # (1st is the best finish), so the sign has to survive.
        out = _pearson(pd.Series([1.0, 2, 3, 4]), pd.Series([4.0, 3, 2, 1]))
        assert out["r"] == pytest.approx(-1.0)

    def test_too_few_points_is_reported_not_raised(self):
        assert _pearson(pd.Series([1.0]), pd.Series([2.0])) == {"r": None, "p": None, "n": 1}

    def test_constant_input_is_reported_not_raised(self):
        # scipy raises on zero variance; a 14-team league where everyone
        # finished level is degenerate, not an error.
        out = _pearson(pd.Series([5.0, 5, 5, 5]), pd.Series([1.0, 2, 3, 4]))
        assert out["r"] is None

    def test_nan_pairs_are_dropped_before_correlating(self):
        x = pd.Series([1.0, 2, 3, np.nan])
        y = pd.Series([2.0, 4, 6, 100])
        out = _pearson(x, y)
        assert out["n"] == 3
        assert out["r"] == pytest.approx(1.0)


def test_league_analysis_reports_league_shape():
    out = league_analysis(engine)
    assert out["teams_in_league"] > 0
    assert "RB" in out["roster_needs"]
