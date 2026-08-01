# Fantasy Football Draft Toolkit

An end-to-end fantasy football analytics pipeline for a 14-team ESPN league: pull league history from the ESPN API, compute a consistent Value Over Replacement Player (VORP) metric, validate a regression model for next-season projections, and use both live during the draft through a React UI backed by a FastAPI recommendation service.

The public write-up (results, charts, plain-language summary) is published via GitHub Pages: **https://justinmck.github.io/fantasy-football-draft-toolkit/**

This README covers the engineering side: architecture, setup, and methodology.

---

## Architecture

```
ESPN API  →  notebooks/NB01  →  SQLite (data/fantasy_data.db)
                                       │
          ┌──────────────────┬─────────┼──────────────────┐
          │                  │         │                  │
  notebooks/NB02     notebooks/NB03    │         notebooks/NB04
  (data cleaning +   (retrospective    │         (next-season
   position fix)      VORP + charts    │          regression +
                      → docs/charts,   │          ablation +
                        docs/tables)   │          players.json
          │                  │         │          export)
          │                  │  notebooks/NB05    │
          │                  │  (projection       │
          │                  │   accuracy +       ▼
          │                  │   ADP benchmark)  draft-board/public/players.json
          │                  │   standalone         (offline fallback)
                             ▼
                    src/scoring.py  ◄── single VORP/baseline
                             │           implementation, shared
                             ▼           by NB03, NB04, NB05, and
                    src/recommender.py    the live API
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
- **Live draft utility** = `max(VORP_z, 0) × need × timing × confidence`, plus small projected-points and recency tiebreakers. See "What the live tool adds on top of the projection" below.

### What the live tool adds on top of the projection

NB04's ablation and NB05's ADP benchmark both concluded the same thing: the projection is at its ceiling, and a better forecast isn't available. So the live tool's job isn't to out-predict ESPN — it's to apply the three things a projection contains **no information about at all**. Each is a separate multiplier in `score()`, and each is returned to the UI as its own column so a recommendation can be explained rather than just asserted.

**Roster need** (`need_weights`, `open_slots`, `roster_urgency`). Weight rises with the number of open *starting* slots at a position, and escalates as your remaining picks run out — an open slot with twelve picks left is barely a constraint; with two picks left it's the whole decision. Two fixes here matter:

- The FLEX slot used to be invisible, because no player's `position` is literally `"FLEX"`. It's now split evenly across the FLEX-eligible positions, and `src/state.py` allocates a drafted player to their own slot first, then FLEX, then bench depth. Previously a third RB kept reading as though a starting slot were still open.
- `have` can no longer exceed `need`; surplus players are tracked separately as depth.

**Pick timing** (`availability`, `availability_pressure`). The question is always "grab them now, or will they last until I'm back?", so the tool estimates the probability a player survives to your next turn, treating their actual draft slot as `Normal(league_pick_est, σ)` with σ widening deeper into the draft (pick 8 is far more predictable than pick 140). Urgency scales smoothly with `1 − P(available)`.

The old `adp_pressure` was a four-step cliff — a player one pick either side of a threshold got a 17% different multiplier — and, worse, the UI passed `next_pick == current_pick` on your own turn, which collapsed the comparison exactly when it mattered. Both are fixed; `adp_pressure` is retained unchanged for comparison (pass `pressure_fn=adp_pressure` to get the old ranking back).

**Confidence** (`confidence`, `risk_multiplier`). Blends two genuinely different things:

| Source | What it measures | Spread |
|---|---|---|
| NB04 bootstrap interval | how much a Ridge fit wobbles across resamples (*model* uncertainty) | narrow, ~3% |
| NB05 per-position R² | how wrong projections at that position actually turn out to be (*outcome* uncertainty) | wide — QB 0.52, WR 0.37, K −0.03, DST 0.00 |
| NB05 unproven-player factor | how much less reliable a projection is with no prior season behind it | ×0.61 |

The interval alone is not a usable risk signal: measured across the top 40 candidates it moved exactly one adjacent pair. Outcome uncertainty is the quantity a drafter is actually exposed to, so it carries the larger weight. The combined figure discounts value by at most `RISK_AVERSION` (default 20%), which is deliberately bounded — it should break ties between players the board already rates as close, not override a real value gap. The UI exposes it as a "Play it safe" slider; `risk_aversion=0` disables it entirely.

**Unproven players.** A player with no prior-season production on record — a rookie, or someone who didn't record stats last season — has nothing behind their projection but the projection. NB05 measures what that costs: R² 0.282 against 0.463 for veterans over 2021–2025, so their number is worth about 61% as much. Position and unproven status are separate axes and compose multiplicatively, so a rookie TE and a rookie RB don't collapse to the same figure.

Two things this deliberately does *not* do. It doesn't claim they're worse players: the same analysis finds they **out-deliver** their projections by 27.3 VORP on average against 8.2 for veterans, so they're under-projected, not over-projected. The adjustment expresses uncertainty only, which is why setting `risk_aversion=0` makes an unproven player and a veteran with identical projections score identically. And the UI surfaces the upside alongside the discount rather than burying it.

The label is "Unproven" rather than "Rookie" because that's what the flag actually measures. A veteran who missed last season looks the same to the model as a true rookie, and calling them a rookie on the board would be wrong.

All three notebooks persist their contribution to the database (`model_projections` from NB04, `position_reliability` and `rookie_reliability` from NB05) and `src/recommender.py` joins them behind existence checks, so the live tool still runs on a database where none of them has been executed.

**One data caveat.** ADP for an upcoming season isn't published until shortly before the draft, so `resolve_adp_year()` falls back to the most recent season on record — otherwise every player reads as "no market data" all offseason and pick timing goes inert. The API returns `adp_year` so it's clear which market the timing signal came from.

### ADP file formats

FantasyPros changed their export layout for 2026. Earlier seasons have separate `Player` / `Team` / `Bye` columns; 2026 merges all three into one `Player (Bye)` column (`"Jahmyr Gibbs   DET (6)"`), adds CBS/Fantrax/Real-Time sources, and uses an em dash for "this source didn't rank them". `clean_adp()` in `notebooks/utils.py` detects and handles both, splitting the combined column on the last all-caps token before the parenthesised bye week.

Defenses come through that split as `Player="Houston Texans"`, `Team="DST"` — the same shape earlier seasons used — so the existing defense name mapping keeps working across every year without a special case.

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
4. `NB04-draft-board.ipynb` — feature validation, model comparison, feature ablation, and the `players.json` export used as the draft board's offline fallback.
5. `NB05-projection-accuracy.ipynb` — how accurate past projections were, and whether they beat ADP. Standalone: not wired into the published site or the live API, and safe to run or skip independently of the others.

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

Open the Vite dev server URL (default `http://localhost:5173`). The frontend talks to the backend at `VITE_API_URL` (`draft-board/.env.local`, defaults to `http://localhost:8000`).

The board shows, per player: **VORP** and **ADP** (the inputs), **Available** and **Confidence** (the two signals the projection doesn't carry), and **Score** (the ranking). Each row also carries short reason chips — "Fills a big need", "Now or never", "Depth only", "Unproven" — derived from the individual scoring multipliers, so you can see *which* factor is driving a recommendation and disagree with it. The "Play it safe" slider adjusts how much uncertainty discounts a player, live.

**Click any player** for a detail panel: last season's actual production (points, games, targets/carries/attempts), the projection's plausible range, why their confidence is what it is, and a line-by-line breakdown of how their score was built from the four multipliers. It stays in sync as the board re-ranks behind it.

If the backend isn't reachable, the UI falls back to the static `players.json` snapshot exported by NB04. That fallback is **value only** — roster need, pick timing, and the position-reliability half of confidence all require a live session, and the UI says so rather than presenting a partial ranking as the real one.

### Tests

```bash
pytest tests/ -v
```

88 tests covering:

- `tests/test_scoring.py` — VORP/baselines, cross-position dampening, and the three live-tool multipliers (roster need incl. FLEX and urgency, availability/pick timing, confidence and its three sources incl. the unproven-player factor)
- `tests/test_state.py` — draft session slot allocation: own slot → FLEX → bench depth, and picks-remaining accounting
- `tests/test_utils.py` — position recovery from `eligible_slots`, and both ADP file formats
- `tests/test_api.py` — FastAPI routes end-to-end against a fixture SQLite database, including a regression test for a NaN-serialization bug (see below) and the ADP-year fallback

---

## Methodology

### The `position` column was lineup slot, not position

Worth stating up front, because it changes the sample size behind every number below.

`players_stats.position` was populated from ESPN's `lineupSlot` — the roster slot a player
occupied — rather than from their actual position. That is a weekly roster decision, not a
property of the player, so most rows arrived labelled `BE` (bench), `RB/WR/TE` (started at flex),
or a raw unmapped slot id `0`. Across all seasons: 1800 rows as `0`, 519 as `BE`, and only 608
carrying a real position.

Every analysis notebook filters `position.isin(POSITIONS)`, so **~76% of each season was being
silently discarded** — and the surviving quarter was biased toward players good enough to hold a
named starting slot, which is precisely the wrong bias when measuring projection accuracy.

The fix derives position from `eligible_slots`, which lists every slot a player is *allowed* to
fill and so is a genuine property of the player (`position_from_eligible_slots` in
`notebooks/utils.py`, applied in NB02). Matching is on singular slot names only, so a WR eligible
at `['RB/WR', 'WR', 'WR/TE', 'RB/WR/TE']` resolves to `WR`, not `RB`. Validated before adoption:
**99.97% recovery** (the one failure is a punter, whose only eligible slot is `P`) and **100%
agreement** with the existing label on all 608 rows that were already correct. Usable rows per
season went from ~108 to 419–462.

The live draft tool was never affected — `src/recommender.py` reads position from
`next_season_projections`, which is built from a separate lookup. That lookup did improve as a
side effect (players with no mappable position: 91 → 79).

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

The question NB04 asks is: *given only information available before the season starts, how well can we predict a player's actual end-of-season VORP?* This is framed as a **feature-validation** exercise, not the thing that directly drives the live draft score — the live score is still `src/scoring.py`'s need/ADP-aware utility function.

**Features, and the leakage boundary.** Candidates are `proj_vorp` (ESPN's projection as value over replacement), `avg_last_year` (previous season's per-game scoring), `opportunities_last_year` and `opportunities_per_game` (previous season's receiving targets + rushing attempts + passing attempts, total and per game), and `is_rookie`. Every one is knowable on draft day: usage comes from the *previous* season via a self-join, never from the current season's `actual_*` columns, which are the outcome being predicted. ESPN's own `proj_receivingTargets` and siblings are deliberately excluded despite being draft-day-legal — they carry the same 2023 extraction gap as `projected_points` and would bias the training set.

`is_rookie` replaces a silent `fillna(0)`: previously a rookie's `avg_last_year = 0` told the model "played last season, produced nothing", which is the same signal as a healthy scratch. The explicit flag lets it distinguish *unknown* from *zero*.

**2023 is excluded.** 392 of its 480 rows have `projected_points = 0.0` — an extraction gap, not real zeros (Patrick Mahomes 2023 shows `points = 267.0`, `projected_points = 0.0`). The 88 survivors aren't a random sample. 2023 is still read as the prior year supplying 2024's features; only its projections are unusable.

**Multicollinearity.** Checked with variance inflation factors rather than assumed. `opportunities_per_game` (≈ 9.8) and `opportunities_last_year` (≈ 8.2) are the most collinear — unsurprising, since one is the other divided by games — with `avg_last_year` ≈ 5.0, `projected_points` ≈ 4.8, `proj_vorp` ≈ 3.2, `is_rookie` ≈ 1.6. `projected_points` is dropped as a standalone regressor since `proj_vorp` already encodes it. The usage terms are kept, with the explicit caveat that their individual coefficients aren't interpretable at that VIF — the ablation below answers the "does it add value" question on held-out data instead, where collinearity doesn't distort the verdict.

**Validation.** A walk-forward split: earlier seasons train, the most recent completed season (`CURRENT_SEASON`) is held out entirely, and model selection uses `GroupKFold` grouped by year so no season leaks across a fold boundary.

**Model comparison.** Linear Regression, Ridge, and a Random Forest, compared via cross-validated RMSE/MAE/R². Ridge and Linear tie for best (cross-validated RMSE ≈ 48.7 vs. ≈ 50.1 for the Random Forest) and Ridge is selected. Note this flipped once the position fix quadrupled the data — the Random Forest's earlier win was an artifact of the small, biased sample.

**Holdout performance.** RMSE ≈ 56.9, MAE ≈ 42.6, R² ≈ 0.552 on 446 held-out players (2025). The model explains a bit over half the variance in end-of-season VORP from pre-draft information alone.

**Ablation — does any feature earn its place?** Refitting on the same split with feature groups added one at a time:

| Feature set | Holdout R² |
|---|---|
| `proj_vorp` alone | 0.553 |
| `+ avg_last_year` | 0.551 |
| `+ usage` | 0.552 |
| `+ is_rookie` | 0.552 |

**Nothing beats `proj_vorp` alone.** This is a real finding, not a modelling failure: ESPN's projection already incorporates last season's usage, so feeding it back in is redundant, and the residual error is dominated by injury, snap-share change, coaching decisions and touchdown variance — none of which exist in July. The full set is kept for the deployed model because these differences are within noise on a single holdout season and pruning on that basis would be overfitting to 2025 specifically.

One consequence: an earlier version of this README claimed `avg_last_year` surviving as a predictor justified the `RECENCY_WEIGHT` term in `score()`. **The ablation withdraws that claim** — adding `avg_last_year` moves holdout R² by −0.002. The weight may still be defensible as a draft-day tiebreaker, but it is no longer validated by this notebook and should be labelled a preference rather than a finding.

**Uncertainty.** NB04 bootstraps the training data (300 resamples, refit each time) and reports a 90% interval (5th–95th percentile) alongside each point estimate, in the notebook and in the exported `players.json`.

### Projection accuracy, and the ADP benchmark (NB05)

NB05 grades projected VORP against delivered VORP across 2020–2025 (2023 excluded, same gap as above), and — the more decision-relevant question — asks whether the projections beat simply drafting off the market.

**Accuracy.** R² between projected and actual VORP runs 0.34–0.54 by season, with typical error 37–55 VORP points. 2025 was the worst year in the record (R² 0.34) and ran systematically low: players out-delivered their projections by 23.6 VORP on average. By position, QB projections have the largest raw errors (RMSE 79.2) but explain the most variance (R² 0.52), while kickers (−0.03) and defenses (0.00) are effectively unpredictable — projecting them is no better than projecting the average.

**The ADP benchmark.** ADP is a pick number and VORP is points above replacement, so these are compared by Spearman rank correlation against actual VORP, not by error metrics. Drafting is an ordering problem anyway. ADP is only on record from 2022, so the head-to-head covers 2022, 2024 and 2025.

| Scope | `proj_vorp` | ADP |
|---|---|---|
| All players (2022 / 2024 / 2025) | 0.595 / 0.736 / 0.744 | 0.529 / 0.620 / 0.586 |
| Top 50 by ADP | 0.370 / 0.485 / 0.618 | 0.483 / 0.303 / 0.552 |
| Mean of true top 24 identified (of 24) | 12.7 | 12.3 |

**Across the full pool the projections beat the market in every season, comfortably. At the top 50 — the picks that decide a season — it's a coin flip**, and both rankers correctly identify only about half the true top 24. Blending the two ranks never meaningfully beats the better input, which is expected: ADP is set largely by people reading these same projections, so the two aren't independent.

**What this means.** Taken with the ablation, both lines of evidence say the ranking signal is near its ceiling and further prediction work has low expected return. The remaining leverage is in cross-position comparability (`add_vorp_z`), in roster-need and pick-timing logic that the projection doesn't contain at all, and in using the uncertainty intervals to prefer the safer pick when the board is close.

### League-specific draft bias

`src/biases.py` fits a small correction for how *this* league's actual draft picks tend to deviate from national ADP (e.g., a league that reaches for QBs earlier than average), using every prior season on record. This is fit on a single 14-team league across a handful of seasons — a small, noisy sample — so it's applied as a mild, illustrative nudge on top of ADP rather than treated as a statistically robust model on its own.

### Known limitations

- All of the above (regression, bias correction) is trained on one league's history. It won't generalize to other leagues' scoring settings or draft tendencies out of the box.
- Sample size across seasons is small; the holdout evaluation is a single season, not a stable long-run estimate. The ADP benchmark is thinner still — three seasons, two of them consecutive — so a narrow win either way there should be read as noise.
- 2023 is unusable for anything projection-based (`projected_points = 0.0` for 392 of 480 rows) and is excluded from NB04 and NB05 rather than reported with an anomalous R². Recovering it would need a re-pull from ESPN.
- Rookies still have no prior-season stats to draw on; `is_rookie` marks them explicitly so the model treats a zero as *unknown* rather than as *produced nothing*, but it doesn't supply the missing information — it only stops the model misreading its absence.
- The K and D/ST projections carry essentially no predictive signal (R² ≈ 0 in NB05). They're still ranked and exported, since the roster requires them, but the numbers next to them shouldn't be trusted as a basis for spending an early pick.

---

## Testing notes

`tests/test_api.py` includes a regression test for a real bug found via live smoke-testing: `/recommend` used to 500 with `ValueError: Out of range float values are not JSON compliant: nan` whenever a candidate player had no prior-season stats (rookies) or a null pro team — both are legitimate, common cases, not bad data. Fixed in `src/recommender.py` by filling those fields before the API response is built; the test fixture includes a synthetic rookie with exactly this shape of missing data to guard against a regression.

---

## Project structure

```
notebooks/        NB01–NB05 pipeline + shared config.py/utils.py
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
