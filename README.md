# Fantasy Football Draft Toolkit

An end-to-end fantasy football analytics pipeline for a 14-team ESPN league: pull league history from the ESPN API, compute a consistent Value Over Replacement Player (VORP) metric, validate a regression model for next-season projections, and use both live during the draft through a React UI backed by a FastAPI recommendation service.

The public write-up (results, charts, plain-language summary) is published via GitHub Pages: **https://justinmck.github.io/fantasy-football-draft-toolkit/**

This README covers the engineering side: architecture, setup, and methodology.

---

## Architecture

```
ESPN API  →  notebooks/NB01  →  SQLite (data/fantasy_data.db)
                                       │
                    ┌──────────────────┼──────────────────┐
                    │                  │                  │
           notebooks/NB02      notebooks/NB03      notebooks/NB04
           (data cleaning)     (retrospective       (next-season
                                VORP + charts         regression +
                                → docs/charts,         players.json
                                  docs/tables)          export)
                                       │                  │
                                       │                  ▼
                                       │         draft-board/public/players.json
                                       │            (offline fallback)
                                       ▼
                              src/scoring.py  ◄── single VORP/baseline
                                       │           implementation, shared
                                       ▼           by NB03, NB04, and the
                              src/recommender.py    live API
                                       │
                                       ▼
                              src/api.py (FastAPI)
                                       │
                                       ▼
                          draft-board/ (React + Vite + Tailwind)
                                       │
                                       ▼
                              live draft UI in the browser
```

`docs/` is also rendered (via Quarto) into a static site published on GitHub Pages, separate from the live draft tool.

### Why one scoring module

Earlier versions of this project computed "how good is this player" four different ways: hardcoded SQL rank windows in the retrospective notebook, a second hardcoded SQL version feeding an unvalidated regression, a third dynamic version in the backend that the UI never actually called, and a fourth set of ad hoc weights hardcoded directly into the frontend. They disagreed with each other and, in the retrospective notebook's case, one of the SQL branches had a real bug (it returned the replacement player's raw points instead of `points - replacement`).

`src/scoring.py` is now the single definition, used everywhere:

- **Replacement baseline** = the Nth-best player at a position, where `N = teams × starters needed at that position` (plus a share of the FLEX slot for FLEX-eligible positions), read from `notebooks/config.py`'s `TEAMS`/`ROSTER_NEEDS` — not an arbitrary, hand-picked rank cutoff.
- **VORP** = a player's value minus that baseline.
- **Live draft utility** additionally weights VORP by unmet roster need and by how much draft-pick pressure there is to grab the player before your next turn (`adp_pressure` in `src/scoring.py`), plus a small term for recent performance.

---

## Setup

```bash
git clone https://github.com/justinmck/fantasy-football-draft-toolkit.git
cd fantasy-football-draft-toolkit

python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cd draft-board
npm install
cd ..
```

### ESPN API access (for re-pulling data)

`notebooks/NB01-data-collection.ipynb` pulls league data via the [`espn-api`](https://github.com/cwendt94/espn-api) package, which requires:

- Your league ID
- Your `SWID` and `espn_s2` cookies (visible in your browser's dev tools → Application → Cookies, while logged into ESPN fantasy)

Put these in a `.env` file (not committed) rather than hardcoding them:

```
LEAGUE_ID=...
SWID=...
ESPN_S2=...
```

### Running the notebooks

Run in order — each depends on tables/files the previous one produces:

1. `NB01-data-collection.ipynb` — pulls raw league/player/draft data from ESPN into `data/raw/` and `data/fantasy_data.db`.
2. `NB02-data-processing.ipynb` — cleans and normalizes the raw data into the DB tables the rest of the project reads from.
3. `NB03-analysis.ipynb` — retrospective VORP, draft-value charts and tables (exported to `docs/charts/`, `docs/tables/`).
4. `NB04-draft-board.ipynb` — feature validation, model comparison, and the `players.json` export used as the draft board's offline fallback.

### Running the live draft tool

Two processes, run together on draft day:

```bash
# terminal 1 — backend
source .venv/bin/activate
uvicorn src.api:app --reload

# terminal 2 — frontend
cd draft-board
npm run dev
```

Open the Vite dev server URL (default `http://localhost:5173`). The frontend talks to the backend at `VITE_API_URL` (`draft-board/.env.local`, defaults to `http://localhost:8000`). If the backend isn't reachable, the UI falls back to the static `players.json` snapshot exported by NB04 — read-only, but usable.

### Tests

```bash
pytest tests/ -v
```

Covers the scoring/VORP functions (`tests/test_scoring.py`) and the FastAPI routes end-to-end against a fixture SQLite database (`tests/test_api.py`), including a regression test for a NaN-serialization bug (see below).

---

## Methodology

### VORP (retrospective and projected)

Replacement level is defined once, in `src/scoring.py`, as described above, and reused by:

- `NB03` — retrospective VORP against actual season points, feeding the draft-value charts (steals/reaches, VORP vs. final standing correlation, VORP by position).
- `NB04` and `src/recommender.py` — the same baseline logic applied to next-season projected points, for the live draft tool.

### Why doesn't the highest-VORP player always look right?

Raw VORP is points above replacement, and that scale isn't comparable across positions, because
replacement level "falls off a cliff" much more steeply for some positions than others. Take the
2026 projections: QB1 projects at 370.5 points against a QB baseline (QB14, the last startable
QB in a 14-team league) of just 1.9 — a 99.5% drop-off. RB1 projects at 265.8 against an RB33
baseline of 21.5 (a 91.9% drop). WR1 is 300.4 against a WR33 baseline of 69.4 (76.9% drop). Only
one QB starts per team, same as any single starting slot, but because the QB points distribution
is so much steeper, QB1's *raw* VORP comes out enormous relative to RB1 or WR1's — enough that a
purely VORP-ranked board puts the top QB at #1 overall almost every year, independent of how the
league's roster is actually built.

`add_vorp_z()` in `src/scoring.py` corrects for this by rescaling each position's VORP relative
to a reference spread taken from the FLEX-eligible positions (RB/WR/TE) — the positions that
actually compete for the same roster slots (2 RB/WR/TE starters + 1 FLEX), which makes them the
fairest common yardstick. Concretely: `vorp_z = vorp * min(reference_spread / this_position's_spread, 1.0)`.
The `min(..., 1.0)` cap means RB/WR/TE (the reference group) are left essentially unchanged, and
only high-spread positions like QB get dampened — a plain z-score would instead shrink *every*
position down to a similarly tiny scale and let minor terms in the utility formula (like the
small `projected_points` bonus) swamp the ranking instead.

This is additive, not a replacement: `vorp` (the old, undampened calculation) is still computed
and returned everywhere `vorp_z` is — the API response, `players.json`, and the NB04 export all
carry both columns side by side, and NB04 includes an old-vs-new top-15 comparison table so you
can see exactly how much the ranking changes. `score()` (and the live draft tool / offline
fallback board) rank on `vorp_z` by default; `NB03`'s retrospective analysis deliberately keeps
using raw `vorp`, since it's measuring *actual delivered value* by position, not producing a
live-draft ranking, so the two are not meant to agree.

### Regression model (NB04)

The question NB04 asks is: *given only information available before the season starts (projected points, projected VORP, last season's per-game average), how well can we predict a player's actual end-of-season VORP?* This is framed as a **feature-validation** exercise, not the thing that directly drives the live draft score — the live score is still `src/scoring.py`'s need/ADP-aware utility function, now informed by what the regression finds predictive.

**Multicollinearity.** `projected_points` and `proj_vorp` are almost the same signal — VORP is arithmetically derived from projected points minus a baseline — so including both as independent regressors produces unstable, hard-to-interpret coefficients. We check this with variance inflation factors (VIF) rather than assuming it: on the current data, `projected_points` and `proj_vorp` both sit in the moderate range (VIF ≈ 4.3 and 3.9), not the extreme range that would demand dropping a feature outright, but high enough that the notebook documents the correlation explicitly and treats individual coefficients with caution rather than over-interpreting them (`avg_last_year`, by contrast, is largely independent at VIF ≈ 1.5).

**Validation.** Earlier versions fit a model and read its own training-set fit back as if that were a real evaluation — no held-out data at all. NB04 now does a walk-forward split: earlier seasons train, the most recent completed season (`CURRENT_SEASON`) is held out entirely for evaluation, and model selection during training uses `GroupKFold` grouped by year so no player-season ever leaks across a fold boundary within the training set.

**Model comparison.** Linear Regression, Ridge, and a Random Forest are compared via cross-validated RMSE/MAE/R² on the training years. On this data the Random Forest wins narrowly (cross-validated RMSE ≈ 63.5 vs. ≈ 65.4 for Linear/Ridge) and is selected as the final model.

**Holdout performance.** Evaluated once, on the untouched held-out season: RMSE ≈ 57.8, MAE ≈ 46.2, R² ≈ 0.54. In plain terms: the model explains a bit over half the variance in actual end-of-season VORP using only pre-draft information, with a typical miss of roughly 46-58 points — a meaningful signal, not a precise forecast.

**Uncertainty.** Point predictions alone overstate confidence, especially with a small, single-league dataset. NB04 bootstraps the training data (300 resamples, refit each time) and reports a 90% interval (5th–95th percentile of predictions) alongside each player's point estimate, both in the notebook and in the exported `players.json`.

### League-specific draft bias

`src/biases.py` fits a small correction for how *this* league's actual draft picks tend to deviate from national ADP (e.g., a league that reaches for QBs earlier than average), using every prior season on record. This is fit on a single 14-team league across a handful of seasons — a small, noisy sample — so it's applied as a mild, illustrative nudge on top of ADP rather than treated as a statistically robust model on its own.

### Known limitations

- All of the above (regression, bias correction) is trained on one league's history. It won't generalize to other leagues' scoring settings or draft tendencies out of the box.
- Sample size across seasons is small; the holdout evaluation is a single season, not a stable long-run estimate.
- Rookies and other players with no prior-season stats fall back to `0` for the recency feature — a real gap, not an imputed estimate, and the model's confidence interval for those players is correspondingly wider on the low end but not adjusted specifically for "rookie" as its own category.

---

## Testing notes

`tests/test_api.py` includes a regression test for a real bug found via live smoke-testing: `/recommend` used to 500 with `ValueError: Out of range float values are not JSON compliant: nan` whenever a candidate player had no prior-season stats (rookies) or a null pro team — both are legitimate, common cases, not bad data. Fixed in `src/recommender.py` by filling those fields before the API response is built; the test fixture includes a synthetic rookie with exactly this shape of missing data to guard against a regression.

---

## Project structure

```
notebooks/        NB01–NB04 pipeline + shared config.py/utils.py
src/               FastAPI backend: api.py, recommender.py, scoring.py,
                   biases.py, state.py, schemas.py, settings.py, db.py
draft-board/       React + Vite + Tailwind live draft UI
data/              raw/ ESPN pulls + fantasy_data.db (SQLite)
docs/              Quarto site source + rendered output (GitHub Pages)
tests/             pytest suite for scoring and the API
```

---

## License

MIT — see [LICENSE](LICENSE).

## Author

Justin McKendry
