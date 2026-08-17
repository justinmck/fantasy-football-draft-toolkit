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

client = TestClient(app)


def test_analysis_endpoint_ok_without_draft_history():
    """The fixture DB has no `players`/`teams` tables - this must not raise."""
    res = client.get("/analysis")
    assert res.status_code == 200
    body = res.json()
    assert body["draft_performance"]["teams"] == []
    assert body["draft_performance"]["correlation"] is None
    assert body["steals_and_reaches"]["steals"] == []
    assert body["projection_value"]["correlation"] is None


def test_analysis_still_returns_reliability_tables():
    """position_reliability exists in the fixture, so it should come through
    even though the draft-history half of the payload is empty."""
    body = client.get("/analysis").json()
    assert len(body["position_reliability"]) > 0
    assert {"position", "r2", "reliability"} <= set(body["position_reliability"][0])


def test_analysis_accepts_year_override():
    res = client.get("/analysis", params={"year": 2021})
    assert res.status_code == 200
    assert res.json()["year"] == 2021


def test_analysis_cache_is_keyed_on_the_year():
    """The season switcher hits this endpoint once per year on the same
    database. A cache keyed only on the database's identity would serve the
    first year asked for to every subsequent request, and the page would look
    frozen while the chips moved."""
    first = client.get("/analysis", params={"year": 2021}).json()
    second = client.get("/analysis", params={"year": 2022}).json()
    again = client.get("/analysis", params={"year": 2021}).json()
    assert first["year"] == 2021
    assert second["year"] == 2022
    assert again["year"] == 2021


def test_analysis_payload_is_json_safe():
    """No NaN anywhere: it isn't valid JSON and would 500 the endpoint, which
    is exactly the bug the recommender already carries a regression test for."""
    raw = client.get("/analysis").text
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
