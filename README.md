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
                             │         └── src/analysis.py
                             │             (retrospective league
                             │              analysis → Analysis tab)
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

**Roster need** (`need_weights`, `open_slots`, `roster_urgency`, `depth_needs`). Two separate claims, added: an unfilled *starting* slot, and a *bench* spot worth having.

The starting-slot term rises with the number of open slots at a position and escalates as your remaining picks run out — an open slot with twelve picks left is barely a constraint; with two picks left it's the whole decision. Two fixes here matter:

- The FLEX slot used to be invisible, because no player's `position` is literally `"FLEX"`. It's now split evenly across the FLEX-eligible positions, and `src/state.py` allocates a drafted player to their own slot first, then FLEX, then bench depth. Previously a third RB kept reading as though a starting slot were still open.
- `have` can no longer exceed `need`; surplus players are tracked separately as depth.

The bench term exists because a 16-round draft with 9 starting slots is **7 bench picks — over a third of the draft**, and the need multiplier used to collapse to exactly 1.0 for every position the moment the starting lineup was full. For that entire stretch the board rated a backup RB and a second kicker identically, which is badly wrong: you draft RB depth on purpose and you never draft a second kicker.

How much a bench spot is worth is derived rather than guessed, from two things:

| | |
|---|---|
| **How often the position's starters miss games** | Measured over 2020–2025 on the top `starters_needed(pos)` players by season points — the same replacement-level tier the VORP baseline uses. RB 10.2%, TE 8.6%, WR 7.6%, QB 4.3%, K 2.3%, DST 1.2%. |
| **How many of them you start** | Starting two RBs plus a share of FLEX is ~2.4× the injury exposure of a single QB, so the same per-player miss rate implies far more depth need. |

Multiplied and normalised, that gives **RB 1.00, WR 0.75, TE 0.49, QB 0.18, K 0.10, DST 0.05** — a backup RB is worth about twenty times a backup DST. Three guards keep it honest: the term is capped below `NEED_WEIGHT` so a backup can never outrank filling a hole in the lineup; it decays as `1 / (1 + already_held)` so the board won't stack six running backs; and it's zero when there's no bench room left. `bench_remaining()` also reserves a pick for every still-open starting slot before counting anything as spare.

Rates are recomputed by `notebooks/compute_availability.py` into the `position_availability` table, read behind an existence check with measured defaults in `src/scoring.py` — so this is an optional refinement, not a required pipeline step.

`score()` returns `start_weight` and `bench_weight` separately, not just their sum, because they mean opposite things to a drafter. The UI uses the split to say "Fills a big need" vs. "Useful depth" vs. "Weak bench spot" rather than describing a valuable third RB and a wasted second kicker with the same words.

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

### Refreshing next-season projections

```bash
python notebooks/pull_projections.py --year 2026     # ESPN → data/raw/2026/…
python notebooks/rebuild_projections.py --year 2026  # → next_season_projections
```

Run these each August, before the draft. `pull_projections.py` unions free agents with every team's roster; `rebuild_projections.py` is NB02's projection step on its own, so refreshing projections doesn't require re-running the whole notebook (whose ADP insert is explicitly not safe to repeat). Re-run `NB04` afterwards to regenerate prediction intervals and the offline `players.json`.

**Why this isn't a notebook cell.** NB01 used to pull projections with `league.free_agents(size=500)`, which is correct *only* while the league sits in its pre-draft state. Run at any other time, `free_agents()` structurally excludes every rostered player — precisely the elite tier. The 2025 pull was made mid-season, so the resulting file contained **none of the top 50 players by ADP**: the draft board recommended Malik Nabers (ADP 37) first overall because Gibbs, Bijan, Chase, Nacua and 44 others were never in the candidate pool at all. Nothing about the scoring was wrong; it was ranking the wrong set of players. Unioning rosters in makes the pull correct whenever it runs.

The rebuild also recovers position from ESPN's `eligible_slots` when a player has no ADP or prior-season history, instead of dropping them. The notebook dropped those rows — which silently excluded incoming rookies, the exact players the "Unproven" flag exists to surface.

### Running the notebooks

Run in order — each depends on tables/files the previous one produces:

1. `NB01-data-collection.ipynb` — pulls raw league/player/draft data from ESPN into `data/raw/` and `data/fantasy_data.db`. (Projections are pulled by `pull_projections.py` above, not by this notebook — see why, there.)
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

Above the list sit three one-line strips: **draft status** (pick and round, whose turn, how far until yours, which starting slots are open by name, sync state), **board ranking** (the top three as clickable pills, each with a reason chip), and a **recent-picks ticker**. Together they take less room than the single hero card they replaced.

The list contains **every** player, with the board's own pick marked by a trophy and an emerald edge wherever it lands in the current sort. That's the point: a recommendation displayed *outside* the list it heads can't show you where it sits in any other ordering, so sorting by ADP used to hide it entirely.

**Click any player** to expand them in place — name, reason chips, the four headline numbers, and the availability/confidence meters. **Full details** opens the side panel with the rest: last season's actual production (points, games, targets/carries/attempts), the projection's plausible range, why their confidence is what it is, and a line-by-line breakdown of how their score was built. Both stay in sync as the board re-ranks behind them.

The **"Play it safe" slider** names the positions it's currently discounting and by how much, rather than showing a bare percentage. Its effect is genuinely uneven — K and DST get cut hard because their projections have historically explained none of the variance in what those players delivered, while skill positions sit within a few percent of each other — and a control whose consequences can't be seen may as well not exist. When the spread is too small to reorder anything, it says so.

If the backend isn't reachable, the UI falls back to the static `players.json` snapshot exported by NB04. That fallback is **value only** — roster need, pick timing, and the position-reliability half of confidence all require a live session, and the UI says so rather than presenting a partial ranking as the real one.

### Signing in, and being remembered

The app used to read credentials from `.env`, which meant there was no sign-in and nothing to remember. Now the home screen asks for your `SWID` and `espn_s2` once.

**The browser never holds the credentials.** They go to the backend, which stores them and returns an opaque device token; only that token is kept in `localStorage`. A script on the page cannot read the ESPN session cookies, and they don't travel on every request. That indirection is the entire design — a simpler version would put both cookies straight into browser storage.

- `POST /auth/connect` validates the pair by calling ESPN's fan endpoint before storing anything, so a bad cookie fails immediately and legibly rather than later as a mysteriously empty board. The same call returns your leagues, so signing in costs one request rather than two. A 404 there means a bad SWID (it's in the URL path) and is reported as such, not as "could not reach ESPN".
- `GET /auth/session` validates a remembered token on load; `POST /auth/forget` signs the device out.
- Tokens live in `data/device_tokens.json` — gitignored, created `0600` before anything is written to it, never logged and never returned. A file rather than an in-memory dict, because the point of the feature is that reopening the app just works, and an in-memory store would sign you out on every backend restart.
- **`.env` remains a fallback**, so the notebooks and a fresh checkout keep working with no sign-in at all.

Reopening the app goes straight to the last league's board. The device remembers `{token, lastLeagueId}`, the league is only remembered after a connect actually succeeded, and a league that has since disappeared from the account falls back to the picker rather than a broken restore. **The league dropdown in the header** switches leagues in place — a new session each time, because team count, rounds and roster need all belong to the league.

### Live ESPN draft sync

**Home lists every league your credentials can reach** — discovered from your SWID via ESPN's fan endpoint, so no league ids are typed by hand. Each row shows when that league's draft is and whether league-specific timing applies to it. The title in the header is always the way back; mid-draft it confirms first, since the picks only live in server memory.

Roster settings are read per league from `rosterSettings.lineupSlotCounts` rather than assumed. All three of the current leagues happen to share the standard shape, but roster need sets replacement level and therefore every VORP on the board — a league starting two quarterbacks would otherwise be scored against a one-QB baseline.

**Draft scheduling.** `draftSettings.date` is simply absent when a commissioner hasn't set one — ESPN sends no null and no sentinel — so that absence is the entire "not scheduled" signal and nothing else is invented for it. A connected league gets a **Draft day** tab led by a live countdown, followed by every pick number you own, the starting slots still to fill, and round 1 by team name. It's a tab rather than a mode, so a league with no date still shows the full board — it gets "Not scheduled yet" in the countdown's place, with the note that the order below is already fixed. Leading zero units are dropped, and seconds appear only inside the final hour, which is also the only time the clock ticks that fast: a seconds digit that moves twice a minute reads as a broken page rather than a slow one. The countdown never flips the app into live mode: that is driven by the first real pick appearing, because drafts start late, get paused, and a scheduled time is a plan rather than a fact.

**League bias is scoped to the league it was measured on.** `league_bias_meta` records the fitted league id and `/recommend` gates on it, so a board for a different league gets market ADP timing only and says so. "This league reaches on Eagles" is a fact about one set of drafters; asserting it about people in another league would be inventing a finding. Manual sessions keep the fit, because that path is the fallback for when sync breaks and stripping the signal exactly when the tool is degraded would make the fallback worse.

**Connect to ESPN** attaches the board to the real draft. Picks appear within a few seconds without anyone clicking: yours fill your roster, everyone else's just leave the pool, and you can see what every team has taken. `src/espn_draft.py` + three endpoints (`/espn/connect`, `/espn/sync/{id}`, `/espn/disconnect`).

Three properties of ESPN's payload drive the design, each verified against the live API:

- **The pre-draft response is not empty.** It already carries every pick slot — 224 for a 14-team, 16-round draft — each with `playerId: -1` and its `teamId` assigned. The full snake order is knowable *before* the draft starts, so `current_pick`/`next_pick` are derived from the real order rather than guessed, which also survives traded picks and keepers. The "has this pick happened" test is `playerId != -1`; anything checking `if picks:` concludes the draft finished before it began.
- **It returns the whole pick list every time, not a delta.** Session state is therefore a pure function of the latest payload: a missed poll costs nothing. That is why the frontend polls a pull-through endpoint with no background worker, and why reconciliation needs only two paths — apply the tail when the completed picks still start with everything already applied, otherwise rebuild by replaying. Undo, reordering, duplicates and manual divergence all collapse into the rebuild.
- **It honours `If-None-Match`**, so most polls are a 0-byte 304. Polling is every 5s, paused when the tab is hidden, stopped when the draft completes or a cookie expires.

**Two things this deliberately does not do.** It doesn't use `espn-api`'s `League.draft` — that gates on `draftDetail.drafted`, a *completion* flag, so it returns nothing for the entire live draft (it also duplicates on refresh and raises `NameError` on `refresh_draft(refresh__teams=True)`). And `src/espn_draft.py` never imports `espn_api` even transitively, because its `ESPNAccessDenied` formats `espn_s2` and `swid` into the exception message; a test asserts the module stays out of `sys.modules`, which is a structural guarantee rather than a promise to be careful.

Ownership is decided by `teamId`, never `memberId` — autodrafted picks carry no `memberId`, and there were 74 of them in the 2025 draft. Your team is resolved once from `owners` membership rather than `primaryOwner`, since a co-owned team lists several owners and the primary one may be somebody else.

A failed sync returns 200 with `status: "auth"` or `"stale"` rather than an error: the sync failed, not the request, and losing the board mid-draft because a cookie expired would be worse than the expiry. **Disconnect** is always available and hands control straight back to the manual buttons.

**Rehearsal.** `notebooks/export_draft_fixture.py` builds `tests/fixtures/espn_draft_2025.json` from the `drafts` table — which has no `memberId` and no SWID, so there is nothing to scrub, by construction rather than by redaction. `tests/test_espn_draft.py` replays the real 2025 draft through the pipeline pick by pick and asserts every pick is detected once, ownership matches, the final roster is full with 7 bench, batched polling lands identically to single-stepping, re-applying is inert, and a rewind rebuilds correctly.

### Sorting the board

Column headers are sortable — click to sort, click again to reverse. First click is descending for quantities where bigger is better and ascending for ADP (pick 1 is best) and names. Two details that matter:

- **The top card never re-sorts.** It always shows the board's own verdict, not whatever floated to row 1 — otherwise sorting by ADP would relabel the earliest-drafted player as "best available for you", which is precisely the claim the tool exists to argue against.
- **A custom sort announces itself.** A banner says the table is no longer the recommended order, with a one-click way back. Letting a sorted table silently look like a ranking would undo the point of the score column.

Missing values always sort last regardless of direction: a player with no ADP isn't "the earliest pick".

### The Analysis tab

The second tab is the entire analytical case behind the board — all five notebooks in one page, with the methodology alongside the numbers. Served from `GET /analysis` (`src/analysis.py`), **for the league you're currently on**.

**It's something you run, not something that appears.** Opening the tab shows a gate first: what the analysis would tell you, which of those parts need prior seasons, and which seasons this league actually has (`GET /analysis/status`, a local read — opening the tab costs nothing). Then a Run button.

- A league whose history is already stored renders immediately; "Re-pull from ESPN first" is the explicit way to refresh it.
- A league with nothing stored runs a **background job** (`POST /analysis/run` → `GET /analysis/job/{id}`), because pulling several seasons of box scores is a couple of minutes and a blocking request risks timing out with the database half-populated. Progress is per season — "Pulling 2023…" — and one season that 404s is skipped and reported rather than losing the four that worked. A pull already running is rejoined rather than started twice.

**Analysis is scoped to one league, and adjusted to its settings.** Every history query filters on `league_id`, so three leagues in one database never pool their rows. `players_stats` is per league because scoring settings genuinely differ (McFL has 37 scoring items; the other two have 46), so the same player-season is worth different points in each — `average_draft_position` stays shared, since it's the national market. Replacement level comes from the connected league's own `lineupSlotCounts`, which moves every VORP on the page. And the season shown follows the league's own drafts rather than the calendar, since a league that skipped a year or drafted offline would otherwise render an empty page.

**A league with no completed drafts still gets most of the page.** Projection accuracy, per-position reliability, unproven players and the ADP benchmark are graded against projections and actual scoring, and don't depend on who drafted. The four that do — who drafted well, draft capital, steals and reaches, and league bias — say plainly that they need seasons this league hasn't played, instead of rendering empty charts. A blank chart under "did drafting matter?" reads as *no* rather than as *not measurable here*.

**Nothing on it is hardcoded.** Figures are either computed at request time or read from a table the notebooks persist. That's deliberate: the notebooks rewrite those tables whenever the data is refreshed, and a number typed into the frontend would silently drift away from what the board is actually doing — which is the exact failure the page exists to prevent. Twelve sections:

1. **The data** — row counts and season coverage per table, plus the two data problems worth knowing about (the `position`-was-really-`lineupSlot` bug that was dropping 76% of every season, and why 2023 is excluded outright).
2. **Replacement level** — what VORP is actually measured *over*, per position, derived from league settings rather than hand-picked rank cutoffs.
3. **Did drafting well matter?** — team draft VORP against final standing, with the correlation and its sample size. The premise the whole tool rests on.
4. **Draft capital** — average VORP and hit rate by round.
5. **Steals and reaches** — where the market was most wrong, both directions.
6. **Projection accuracy** — R², RMSE and signed bias, overall and per season, in VORP terms.
7. **Reliability by position** — why kickers and defenses are treated as coin flips.
8. **Unproven players** — less predictable, but they *out-deliver* their projections.
9. **Beating the market** — Spearman against ADP across three scopes, with the finding that the edge narrows to near-nothing at the top of the board.
10. **The model** — walk-forward split, model comparison, holdout evaluation, the feature ablation showing extra features add nothing, and the VIF diagnostic behind the feature set.
11. **Bench depth** — missed-game rates and what a bench spot at each position is worth.
12. **The live score** — how the four multipliers combine, and why each is surfaced separately.

NB04 persists its model comparison, ablation and VIF numbers to `model_report` / `model_ablation` / `feature_vif` for this page (rather than the frontend restating them), the same way NB05 already persisted its reliability tables. Every read is guarded by table *and column* existence checks, so a partially built database renders "not available" rather than 500ing.

### Performance

The database was built entirely by `to_sql`, which creates **no indexes**, and the Analysis tab rebuilt everything from scratch on every tab switch. Measured before and after:

| | Before | After |
|---|---|---|
| `/analysis` first load | 2.8 s | **0.35 s** |
| `/analysis` repeat | ~500 ms | **~22 ms** |
| `/analysis` over the wire | 113 KB | **22 KB** |
| `/recommend` payload | 276 KB | **48 KB** |
| Initial JS bundle | 293 KB | **241 KB** |

What actually mattered, in order:

- **`src/indexes.py`.** One expression index on `average_draft_position(year, CAST(player_id AS INTEGER))` took the hottest join from 57 ms to 1.4 ms. It's created from NB02's **final** cell, `notebooks/create_indexes.py`, and API startup — never the schema cell, because `to_sql(if_exists="replace")` drops each table *along with its indexes*. The CAST must be spelled identically at both query sites or SQLite silently stops using it; both carry a comment saying so.
- **A request-scoped `_Ctx` in `src/analysis.py`.** `load_draft_season` was running four times per request and `_projection_actuals` twice, returning identical frames, plus 35 separate connections just to ask whether a table exists. Deliberately request-scoped rather than process-scoped: the notebooks drop and recreate these tables, so a longer-lived cache would serve numbers from a database that no longer exists.
- **An `/analysis` response cache keyed on the database file's mtime and size.** The endpoint previously carried a comment explaining why it refused to cache; that reasoning was right, so the key satisfies it rather than overriding it — the moment a notebook writes, the key changes and the entry is dropped. Fails open (no caching) for non-file database URLs.
- **`GZipMiddleware`.** `/recommend` ships up to 300 players × 34 columns of highly repetitive JSON on every pick; it compresses about 6:1.
- **scipy imported at module level.** The lazy import inside `_pearson` cost 1.2 s on the first request and bought nothing — scipy is a hard dependency that scikit-learn pulls in anyway. It stays a dependency rather than being hand-rolled: only `pearsonr` is used, and while `r` is a numpy one-liner, the p-value needs an incomplete beta at `df = n−2`, and `draft_performance` correlates over 14 teams — exactly where a normal approximation is wrong.
- **Frontend:** the Analysis tab is `React.lazy`-loaded into its own chunk and kept mounted once visited (unmounting threw away the payload, so every switch back refetched); `PlayerRow` is memoised with identity-stable handlers; the search box is debounced 150 ms, splitting the bound input from the value the filter reads.

Deliberately **not** done: virtualising the player table. 300 memoised rows is fine, and every library for it breaks `<table>` semantics, the sticky header, and the expand-in-place row.

### Tests

```bash
pytest tests/ -v
```

239 tests covering:

- `tests/test_scoring.py` — VORP/baselines, cross-position dampening, and the three live-tool multipliers (roster need incl. FLEX, urgency and bench depth, availability/pick timing, confidence and its three sources incl. the unproven-player factor)
- `tests/test_state.py` — draft session slot allocation: own slot → FLEX → bench depth, and picks-remaining/bench accounting
- `tests/test_utils.py` — position recovery from `eligible_slots`, and both ADP file formats
- `tests/test_api.py` — FastAPI routes end-to-end against a fixture SQLite database, including a regression test for a NaN-serialization bug (see below) and the ADP-year fallback
- `tests/test_espn_draft.py` — live draft sync: the pre-draft payload having every slot but no picks, `draftDetail.drafted` being ignored, ownership via `owners` membership across brace/case spellings, autodrafted picks still attributing to a team, and a full replay of the 2025 draft (single-stepped, batched, re-applied and rewound). Also pins that the module never imports `espn_api`
- `tests/test_espn_sync_api.py` — the endpoint contract: picks applying without a click, a failed sync degrading to a status instead of an error, the poll throttle, and that no credential appears in any response body
- `tests/test_biases.py` — league draft bias: additive shifts, junk/unknown NFL teams, the no-market sentinel staying unshifted, `bias_reason` being `None` rather than NaN, empirical-Bayes shrinkage edge cases, and a regression test pinning that the fit reads position from ADP rather than from the draft lineup slot
- `tests/test_analysis.py` — the retrospective analysis endpoint: correlation edge cases (too few points, zero variance, NaN pairs), JSON-safety of the payload, and the degradation path when the draft-history tables aren't present

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

Every player carries a national ADP, but a league drafts to its own habits. `src/biases.py` measures those habits across **996 picks over 2020–2025** and turns them into a **pick-timing** adjustment — `league_pick_est`, which feeds the availability and urgency multipliers.

**Timing only, never value.** This league taking Philadelphia players sixteen picks earlier than the market doesn't make them better players; it means they'll be gone sooner, so you have to act earlier. Nothing here touches VORP, projected points or confidence.

What's measured, and what's actually applied:

| Effect | Raw | Applied | Evidence |
|---|---|---|---|
| **QB** taken early | −11.8 | **−11.1** | t = −5.6, same direction all six seasons |
| **TE** taken early | −6.6 | −6.2 | t = −2.7 |
| **DST / K** last longer | +8.1 / +7.1 | +7.1 / +6.1 | t = 2.6 / 2.0 |
| **RB / WR** | +1.2 / +2.9 | ~0 | no meaningful habit |
| **PHI** taken early | −16.1 | **−9.0** | t = −5.1, every season, most managers |
| **NE** taken early | −9.8 | −5.1 | t = −2.9 |
| Managers (3 of 14) | ±5–8 | **not applied** | family-wise p ≈ 0.002 |
| Individual players | ±20–45 | **not applied** | n = 2–4 against σ ≈ 20 picks |

Manager effects are real but attach to a *drafter*, not a player — the tool has no idea which of the other thirteen managers is on the clock, so there's nothing to attach the shift to. Per-player effects are mostly noise, and the strict filter that selects survivors runs on the same data that produced them. Both are fitted, persisted and shown in the Analysis tab; neither moves the board.

**Method.** ADP is capped at 180 (the length of the board) because anyone ranked past it can only ever produce a large negative delta with no possible positive counterpart — a truncation artefact that drags every mean negative and inflates the spread by 50–80%. Deltas are demeaned by season (the league went 12→14 teams in 2024). Team and manager effects are residualised on position×year first, so "this manager reaches" isn't just "this manager drafts quarterbacks". Managers are keyed on `teams.team_id`, never `team_name` — "Gerald Pea's Football Team" (id 11) and "GeraldPea's Football Team" (id 12) are *different* franchises one space apart.

Estimates are shrunk toward zero by empirical Bayes (`shrunk = mean × n/(n+k)`, k fitted per family) rather than gated on significance: a hard `|t| > 2` cutoff would give a team with n=11 the same weight as one with n=41. This is honest but blunt — it takes Philadelphia from −16 to −9, which is correct under a mostly-null model across 32 teams and is reported alongside the raw mean rather than quietly swapped for it.

```bash
python notebooks/compute_league_bias.py --permutations 2000
```

Writes `league_bias_position` / `_proteam` / `_manager` / `_player` / `_meta` (plus per-season breakdowns). `src/api.py` reads these at startup via `load_league_bias` and falls back to measured constants, so the tool runs on a database where the fit has never been executed.

**What this replaced.** The previous implementation fitted `k = median(actual_pick / market_adp)` and applied `k × adp + offset`. On real data `k` came out to exactly 1.0 — a knob that looked like it did something and didn't. Worse, it grouped by `drafts.position`, which is ESPN's *lineup slot*: 358 of 800 training rows were `"BE"` and were silently discarded by a `.get(pos, 0.0)`, so the RB offset was fitted only on RBs who happened to start at RB, systematically excluding the late and bench picks where reaching actually shows up. Same lineup-slot-versus-position bug documented above for `players_stats`.

**In the board.** A reason chip sits directly after the urgency chip (it explains that number): "League reaches here", "Goes early here", "Lasts longer here", with the arithmetic and the timing-only caveat in the tooltip. The detail panel shows `ADP 42 → your league ≈ 32 (−10)`. The ADP column keeps showing the **market** number — putting a league-adjusted value under a header labelled "ADP" would be the same dishonesty the custom-sort banner exists to prevent.

### Known limitations

- All of the above (regression, bias correction) is trained on one league's history. It won't generalize to other leagues' scoring settings or draft tendencies out of the box. The bias fit in particular is 996 picks from 14 managers — enough for the position and top team effects, not enough to trust anything smaller.
- **Only one league's bias fit is stored at a time.** The `league_bias_*` tables are replaced wholesale, so fitting league B discards league A's fit. It's tolerable because the app fits the league you're looking at and `league_bias_meta.league_id` records which one that was — the Analysis tab and the board both check it and fall back to market ADP rather than attributing one league's habits to another — but re-running is the only way back.
- `players_stats` is per league, which makes the recommender's join one-to-many unless a league is named. It resolves to one league's rows (the one being drafted, or the fullest stored league for a brand-new one); a database predating the `league_id` migration is left unfiltered, which is the old behaviour exactly.
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
