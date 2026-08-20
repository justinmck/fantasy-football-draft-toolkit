import React, { useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertTriangle,
  BarChart3,
  ChevronLeft,
  ChevronRight,
  Crown,
  Database,
  Fingerprint,
  Gem,
  Layers,
  Ruler,
  ShieldCheck,
  Target,
  TrendingDown,
} from "lucide-react";

import { fmt, fmtAdp } from "../theme";
import ShareButton, { drawRankedCard } from "./share";
import { PositionChip } from "./primitives";

/**
 * The full analytical case behind the board — all five notebooks in one page.
 *
 * Every number is fetched from `/analysis` rather than written into this file.
 * The notebooks rewrite the underlying tables whenever the data is refreshed,
 * so a hardcoded figure here would drift away from what the board is actually
 * doing, which is the exact failure this page exists to prevent.
 */

// ---------------------------------------------------------------------------
// primitives
// ---------------------------------------------------------------------------

const Section = ({ id, icon, eyebrow, title, blurb, action, children }) => (
  <section id={id} className="card scroll-mt-20 p-5 sm:p-6">
    {eyebrow && <div className="label mb-1.5 text-accent-hover">{eyebrow}</div>}
    <div className="mb-2 flex items-start gap-2">
      <span className="mt-0.5 shrink-0 text-emerald-400">{icon}</span>
      <h2 className="text-lg font-semibold tracking-tight text-ink">{title}</h2>
      {/* Share sits on the title row of the sections worth sending, so it
          reads as part of the finding rather than a toolbar over the page. */}
      {action && <div className="ml-auto">{action}</div>}
    </div>
    {blurb && <div className="mb-5 max-w-3xl text-sm leading-relaxed text-ink-muted">{blurb}</div>}
    {children}
  </section>
);

const Sub = ({ children }) => (
  <h3 className="mb-2 mt-6 text-sm font-semibold text-ink first:mt-0">{children}</h3>
);

const Note = ({ children }) => (
  <p className="mt-3 max-w-3xl text-xs leading-relaxed text-ink-faint">{children}</p>
);

/** A conclusion, stated plainly. Used sparingly — one per section at most. */
const Verdict = ({ tone = "calm", children }) => {
  const tones = {
    calm: "border-emerald-400/20 bg-emerald-500/5 text-emerald-200/90",
    warn: "border-amber-400/20 bg-amber-500/5 text-amber-200/90",
    info: "border-sky-400/20 bg-sky-500/5 text-sky-200/90",
  };
  return (
    <p className={`mt-4 max-w-3xl rounded-lg border px-3.5 py-2.5 text-sm leading-relaxed ${tones[tone]}`}>
      {children}
    </p>
  );
};

const Figure = ({ value, label, tone = "text-ink" }) => (
  <div className="rounded-xl border border-line/60 bg-surface-raised px-4 py-3">
    <div className={`tabular text-2xl font-semibold ${tone}`}>{value}</div>
    <div className="mt-0.5 text-xs leading-snug text-ink-faint">{label}</div>
  </div>
);

const Figures = ({ children, cols = 3 }) => (
  <div className={`grid gap-3 sm:grid-cols-2 lg:grid-cols-${cols}`}>{children}</div>
);

/** Signed bar growing from a shared center line, for +/- quantities. */
const DivergingBar = ({ value, max }) => {
  const half = Math.min(Math.abs(value) / (max || 1), 1) * 50;
  const positive = value >= 0;
  return (
    <div className="relative h-2 w-full overflow-hidden rounded-full bg-surface-hover">
      <div className="absolute inset-y-0 left-1/2 w-px bg-line-strong" />
      <div
        className={`absolute inset-y-0 rounded-full ${positive ? "bg-emerald-400" : "bg-rose-400"}`}
        style={positive ? { left: "50%", width: `${half}%` } : { right: "50%", width: `${half}%` }}
      />
    </div>
  );
};

const ShareBar = ({ value, tone = "bg-sky-400" }) => (
  <div className="h-2 w-full overflow-hidden rounded-full bg-surface-hover">
    <div
      className={`h-full rounded-full ${tone}`}
      style={{ width: `${Math.max(0, Math.min(1, value || 0)) * 100}%` }}
    />
  </div>
);

const Table = ({ head, children, align = [] }) => (
  <div className="overflow-x-auto">
    <table className="w-full border-collapse text-sm">
      <thead>
        <tr className="text-ink-faint">
          {head.map((h, i) => (
            <th
              key={h}
              className={`py-2 pr-3 text-[11px] font-medium uppercase tracking-wider ${
                align[i] === "right" ? "text-right" : "text-left"
              }`}
            >
              {h}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>{children}</tbody>
    </table>
  </div>
);

const ordinal = (n) => {
  if (n == null) return "—";
  const s = ["th", "st", "nd", "rd"];
  const v = n % 100;
  return n + (s[(v - 20) % 10] || s[v] || s[0]);
};

const pctOf = (v, digits = 0) =>
  v == null || !Number.isFinite(Number(v)) ? "—" : `${(Number(v) * 100).toFixed(digits)}%`;

function correlationVerdict(corr) {
  if (!corr || corr.r == null) return null;
  const a = Math.abs(corr.r);
  return {
    strength: a >= 0.7 ? "strong" : a >= 0.4 ? "moderate" : "weak",
    significant: corr.p != null && corr.p < 0.05,
  };
}

const SECTIONS = [
  { id: "data", label: "The data" },
  { id: "vorp", label: "Replacement level" },
  { id: "draft", label: "Did drafting matter?" },
  { id: "rounds", label: "Draft capital" },
  { id: "bias", label: "How this league is odd" },
  { id: "market", label: "Steals & reaches" },
  { id: "accuracy", label: "Projection accuracy" },
  { id: "positions", label: "Reliability by position" },
  { id: "rookies", label: "Unproven players" },
  { id: "benchmark", label: "Beating the market" },
  { id: "model", label: "The model" },
  { id: "bench", label: "Bench depth" },
  { id: "live", label: "The live score" },
];

// ---------------------------------------------------------------------------

// What the analysis is for, said before it's run rather than after. Each of
// these maps to a section below, and the ones that need prior seasons are
// marked, because that's the difference between a full page and half of one.
const PROMISES = [
  { icon: <Crown size={14} />, needsHistory: true,
    title: "Who actually drafted well",
    body: "Every manager's picks graded against what those players went on to score — and whether drafting well predicted finishing well." },
  { icon: <Fingerprint size={14} />, needsHistory: true,
    title: "Your league's own habits",
    body: "Positions and NFL teams this league reaches for or lets slide, measured against the national market, and which managers do it." },
  { icon: <Gem size={14} />, needsHistory: true,
    title: "Steals and reaches",
    body: "The picks that returned far more or far less than their draft slot was worth, and what each round is really worth here." },
  { icon: <Target size={14} />, needsHistory: false,
    title: "How much the projections are worth",
    body: "ESPN's projections graded against what happened, overall and position by position — the reliability the board's confidence column is built on." },
  { icon: <Ruler size={14} />, needsHistory: false,
    title: "Where the market was wrong",
    body: "Average draft position against actual value, plus how unproven players and rookies really performed." },
];

/** The screen before the page: what you'd get, what it needs, and the button. */
function RunGate({ leagueName, status, onRun, job, error }) {
  const seasons = status?.seasons || [];
  const has = seasons.length > 0;
  const running = !!job && job.status === "running";

  return (
    <div className="mx-auto max-w-3xl p-4">
      <div className="card p-6 sm:p-8">
        <div className="label mb-1.5 text-accent-hover">Analysis</div>
        <h1 className="text-xl font-semibold tracking-tight text-ink">
          Analyze {leagueName || "this league"}
        </h1>
        <p className="mt-2 text-sm leading-relaxed text-ink-muted">
          This reads your league's completed drafts and finished seasons out of ESPN and grades
          them. Nothing is precomputed — it's built from this league's own history, so the numbers
          describe the people you actually draft against.
        </p>

        <div className="mt-6 space-y-3">
          {PROMISES.map((p) => (
            <div key={p.title} className="flex gap-3">
              <span className="mt-0.5 shrink-0 text-emerald-400">{p.icon}</span>
              <div>
                <div className="text-sm font-medium text-ink">
                  {p.title}
                  {p.needsHistory && (
                    <span className="ml-2 rounded bg-surface-raised px-1.5 py-0.5 text-[10px] text-ink-faint">
                      needs prior seasons
                    </span>
                  )}
                </div>
                <div className="text-xs leading-relaxed text-ink-faint">{p.body}</div>
              </div>
            </div>
          ))}
        </div>

        {/* The honest version of "this might not work": say which seasons are
            there before the button is pressed, not after a two-minute pull. */}
        <div className={`mt-6 rounded-xl border px-4 py-3 text-sm leading-relaxed ${
          has ? "border-emerald-400/20 bg-emerald-500/5 text-emerald-200/90"
              : "border-amber-400/20 bg-amber-500/5 text-amber-200/90"}`}>
          {status === null ? (
            "Checking what this league has…"
          ) : has ? (
            <>
              <strong>{seasons.length} season{seasons.length === 1 ? "" : "s"} stored</strong>{" "}
              ({seasons.join(", ")}). Everything above is available.
              {seasons.length < 3 && (
                <div className="mt-1 text-xs text-emerald-200/60">
                  Thin history — the league-habit figures are measured on a few hundred picks, so
                  treat the smaller effects as suggestive rather than settled.
                </div>
              )}
            </>
          ) : (
            <>
              <strong>No prior seasons stored yet.</strong> Running will ask ESPN which seasons this
              league has and pull them. A league in its first year genuinely has no draft history —
              in that case you'll still get the projection and market sections, and the four that
              grade past drafts will say plainly that they need seasons this league hasn't played.
            </>
          )}
        </div>

        {error && (
          <div className="mt-3 rounded-lg border border-rose-400/30 bg-rose-500/10 px-3 py-2 text-xs leading-relaxed text-rose-300">
            {error}
          </div>
        )}

        {running ? (
          <div className="mt-6">
            <div className="mb-2 flex items-center justify-between text-xs text-ink-muted">
              <span>{job.message || "Working…"}</span>
              <span className="tabular">{Math.round((job.progress || 0) * 100)}%</span>
            </div>
            <div className="h-1.5 w-full overflow-hidden rounded-full bg-surface-hover">
              <div className="h-full rounded-full bg-emerald-400 transition-all"
                   style={{ width: `${Math.max(3, (job.progress || 0) * 100)}%` }} />
            </div>
            <p className="mt-2 text-xs text-ink-ghost">
              Pulling several seasons of box scores takes a minute or two. You can go back to the
              board — this keeps running.
            </p>
          </div>
        ) : (
          <button
            onClick={() => onRun()}
            disabled={status === null}
            className="mt-6 h-11 w-full rounded bg-accent-fill text-sm font-semibold text-white
 transition hover:bg-accent-fill-hover disabled:opacity-50"
          >
            {has ? "Run analysis" : "Run analysis (pulls this league's history)"}
          </button>
        )}

        {has && !running && (
          <button
            onClick={() => onRun({ pull: true })}
            className="mt-2 w-full text-center text-xs text-ink-faint hover:text-ink-muted"
          >
            Re-pull from ESPN first
          </button>
        )}
      </div>
    </div>
  );
}

export default function Analysis({ apiUrl, leagueId, leagueName, myTeamName,
                                  sessionId, authHeaders = {} }) {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [status, setStatus] = useState(null);
  const [job, setJob] = useState(null);
  const [runError, setRunError] = useState(null);
  // null means "whichever season this league last drafted in" - the server
  // resolves that, since it knows which seasons actually have a draft.
  const [year, setYear] = useState(null);
  const [switching, setSwitching] = useState(false);

  const get = (path) => fetch(`${apiUrl}${path}`, { headers: authHeaders });

  // What this league has, before anything is run. Cheap and local — it reads
  // what's stored rather than asking ESPN, so opening the tab costs nothing.
  useEffect(() => {
    let cancelled = false;
    setData(null);
    setStatus(null);
    setJob(null);
    setRunError(null);
    if (!leagueId) return;
    get(`/analysis/status?league_id=${encodeURIComponent(leagueId)}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((s) => {
        if (cancelled) return;
        setStatus(s);
        // A pull started from another tab (or before a reload) is still the
        // same job; rejoin it rather than starting a second one.
        if (s.job) setJob(s.job);
      })
      .catch((e) => !cancelled && setErr(String(e)));
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiUrl, leagueId]);

  const fetchAnalysis = React.useCallback((forYear) => {
    const qs = new URLSearchParams();
    if (leagueId) qs.set("league_id", leagueId);
    if (sessionId) qs.set("session_id", sessionId);   // carries the league's lineup
    if (forYear) qs.set("year", forYear);
    return get(`/analysis?${qs}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((d) => { setData(d); setYear(d.year); })
      .catch((e) => setErr(String(e)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [apiUrl, leagueId, sessionId]);

  // Switching season keeps the current page on screen and dims it, rather than
  // dropping back to the loading state. Every season has the same sections in
  // the same order, so blanking the page throws away the reader's scroll
  // position and their place in a comparison they're part-way through making.
  const showYear = React.useCallback((y) => {
    if (y === year) return;
    setSwitching(true);
    fetchAnalysis(y).finally(() => setSwitching(false));
  }, [year, fetchAnalysis]);

  const run = (opts = {}) => {
    setRunError(null);
    // Already stored and not being force-refreshed: there is nothing to pull,
    // so don't spend two minutes on ESPN to arrive at the same rows.
    if (status?.has_history && opts.pull !== true) {
      setData("loading");
      fetchAnalysis();
      return;
    }
    fetch(`${apiUrl}/analysis/run`, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders },
      body: JSON.stringify({ league_id: String(leagueId) }),
    })
      .then((r) => (r.ok ? r.json() : r.json().then((b) => Promise.reject(new Error(b.detail || r.status)))))
      .then(setJob)
      .catch((e) => setRunError(e.message || String(e)));
  };

  // Poll the job. Stops on its own the moment it isn't running, so a finished
  // pull doesn't leave an interval behind.
  useEffect(() => {
    if (!job || job.status !== "running") return;
    let cancelled = false;
    const id = setInterval(() => {
      get(`/analysis/job/${job.id}`)
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
        .then((j) => {
          if (cancelled) return;
          setJob(j);
          if (j.status === "done") {
            setStatus((s) => ({ ...s, seasons: j.result?.seasons || [],
                                has_history: !!(j.result?.seasons || []).length }));
            setData("loading");
            fetchAnalysis();
          } else if (j.status === "failed") {
            setRunError(j.error || "The pull failed.");
          }
        })
        .catch(() => {});
    }, 1200);
    return () => { cancelled = true; clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job?.id, job?.status, fetchAnalysis]);

  if (err) {
    return (
      <div className="mx-auto max-w-5xl p-4">
        <div className="rounded-xl border border-amber-400/25 bg-amber-500/10 px-4 py-3 text-sm leading-relaxed text-amber-200">
          The analysis needs the backend running — it's computed live from the database rather than
          saved as static charts. Start it with{" "}
          <code className="text-amber-100">.venv/bin/uvicorn src.api:app --reload</code> and reload.
          <div className="mt-1 text-xs text-amber-300/70">{err}</div>
        </div>
      </div>
    );
  }

  // Nothing has been asked for yet - the gate, not a page of numbers the user
  // didn't request and can't tell the provenance of.
  if (!data) {
    return (
      <RunGate leagueName={leagueName} status={status} job={job} error={runError}
               onRun={run} />
    );
  }

  if (data === "loading") {
    return <div className="mx-auto max-w-5xl p-8 text-sm text-ink-faint">Building the analysis…</div>;
  }

  return (
    <div className="mx-auto max-w-5xl p-4">
      <Intro data={data} leagueName={leagueName} />
      {!data.has_history && (
        <div className="mt-4 rounded-xl border border-amber-400/25 bg-amber-500/10 px-4 py-3 text-sm leading-relaxed text-amber-200">
          <strong>This league has no completed prior drafts</strong>, so the four sections that
          grade past drafts — who drafted well, what each round returned, steals and reaches, and
          this league's own habits — have nothing to measure. Everything else on this page is
          graded against projections and actual scoring and doesn't need them.
        </div>
      )}
      {/* Above the contents, because it governs everything under it: the
          retrospective sections all describe one season, and this is which. */}
      {data.seasons?.length > 1 && (
        <div className="mt-4 flex flex-wrap items-center justify-between gap-3 rounded-xl
 border border-line bg-surface-panel px-4 py-3">
          <YearSwitcher seasons={data.seasons} year={data.year} onPick={showYear} busy={switching} />
          {/* Two different things happen when this changes, and saying which
              is which stops the projection numbers looking unresponsive. */}
          <span className="max-w-sm text-xs leading-relaxed text-ink-ghost">
            Sections naming a season show that one. The projection and market sections pool every
            season up to it, so they widen as you move forward.
          </span>
        </div>
      )}
      <Contents />
      {/* Dimmed rather than replaced while a season loads - see showYear. */}
      <div className={`mt-5 space-y-5 transition-opacity ${switching ? "opacity-40" : ""}`}>
        <DataSection data={data} />
        <ReplacementSection data={data} />
        <DraftPerformanceSection data={data} leagueName={leagueName} myTeamName={myTeamName} />
        <RoundsSection data={data} />
        <LeagueBiasSection data={data} />
        <MarketSection data={data} leagueName={leagueName} myTeamName={myTeamName} />
        <AccuracySection data={data} />
        <PositionReliabilitySection data={data} />
        <RookieSection data={data} />
        <BenchmarkSection data={data} />
        <ModelSection data={data} />
        <BenchSection data={data} />
        <LiveScoreSection data={data} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------

/**
 * Season navigation for the retrospective half of the page.
 *
 * The analysis used to show whichever season the league last drafted in and
 * nothing else, which is a strange thing to do with six seasons of history
 * sitting in the database — "did drafting matter?" is a much better question
 * asked of six drafts than of one, and the answer moves year to year.
 */
function YearSwitcher({ seasons, year, onPick, busy }) {
  if (!seasons?.length) return null;
  const i = seasons.indexOf(year);
  const step = (d) => {
    const next = seasons[i + d];
    if (next != null) onPick(next);
  };

  return (
    <div className="flex flex-wrap items-center gap-2">
      <span className="label shrink-0">Season</span>

      {/* Arrows as well as chips: stepping back one year at a time is how you
          actually read a trend, and it's one target instead of aiming at a
          different chip each time. */}
      <div className="flex items-center gap-1 rounded-lg border border-line bg-surface-panel p-1">
        <button
          onClick={() => step(-1)}
          disabled={i <= 0 || busy}
          aria-label="Earlier season"
          className="rounded p-1 text-ink-faint transition hover:text-ink disabled:opacity-25"
        >
          <ChevronLeft size={14} />
        </button>

        {seasons.map((y) => (
          <button
            key={y}
            onClick={() => onPick(y)}
            aria-current={y === year ? "true" : undefined}
            disabled={busy}
            className={`tabular rounded-md px-2 py-0.5 text-xs font-medium transition-colors ${
              y === year ? "bg-surface-hover text-ink" : "text-ink-faint hover:text-ink-muted"
            }`}
          >
            {y}
          </button>
        ))}

        <button
          onClick={() => step(1)}
          disabled={i < 0 || i >= seasons.length - 1 || busy}
          aria-label="Later season"
          className="rounded p-1 text-ink-faint transition hover:text-ink disabled:opacity-25"
        >
          <ChevronRight size={14} />
        </button>
      </div>

      {busy && <span className="text-xs text-ink-faint">Loading…</span>}
    </div>
  );
}

const Intro = ({ data, leagueName }) => (
  <div className="card p-5 sm:p-6">
    <div className="label mb-1.5 text-accent-hover">
      {leagueName || "This league"}
      {data.seasons?.length ? ` · ${data.seasons.join(", ")}` : ""}
    </div>
    <h1 className="text-xl font-semibold tracking-tight text-ink">
      What the board is built on
    </h1>
    <p className="mt-2 max-w-3xl text-sm leading-relaxed text-ink-muted">
      Five notebooks sit behind the draft board. They pull{" "}
      <strong className="text-ink-muted">{leagueName || "this league"}</strong>'s history out of
      ESPN, clean it, measure who actually drafted well, fit and validate a next-season projection,
      and then grade how much that projection was worth. This page is all of it in one place — the
      methods, the numbers, and what they mean.
    </p>
    <p className="mt-3 max-w-3xl text-sm leading-relaxed text-ink-muted">
      Everything below is recomputed from the database each time this page loads, so it can never
      disagree with the board itself. Retrospective figures cover the{" "}
      <strong className="text-ink-muted">{data.year}</strong> season unless a wider range is stated.
    </p>
    {/* The first claim is a measurement, so it's only made where it was
        measured. Stating it for a league with no completed drafts would be
        borrowing another league's result and calling it this one's. */}
    <Verdict tone="info">
      <strong>The short version.</strong>{" "}
      {data.has_history
        ? "Drafting well genuinely predicted finishing well in this league. "
        : "Whether drafting well predicts finishing well can't be checked here yet — that needs completed drafts. "}
      ESPN's projections are decent and beat the market overall, but not reliably at the top
      of the board where picks are actually agonised over. A regression trained to improve on them
      fails to — the projection is at its ceiling. So the board's job isn't to out-forecast anyone:
      it's to apply the three things a projection contains no information about at all, which are
      your roster, the clock, and how much the number can be trusted.
    </Verdict>
  </div>
);

const Contents = () => (
  <nav className="card mt-5 p-4">
    <div className="label mb-2.5">Contents</div>
    <div className="flex flex-wrap gap-1.5">
      {SECTIONS.map((s, i) => (
        <a
          key={s.id}
          href={`#${s.id}`}
          className="rounded-md bg-surface-raised px-2.5 py-1 text-xs text-ink-muted transition
 hover:bg-surface-hover hover:text-ink"
        >
          <span className="tabular mr-1.5 text-ink-ghost">{i + 1}</span>
          {s.label}
        </a>
      ))}
    </div>
  </nav>
);

// --- 1. the data ---------------------------------------------------------

function DataSection({ data }) {
  const tables = data.dataset?.tables || [];
  const broken = data.dataset?.broken_projection_years || [];
  return (
    <Section
      id="data"
      icon={<Database size={17} />}
      eyebrow="NB01 · NB02 — collection and cleaning"
      title="The data, and what's wrong with it"
      blurb={
        <>
          League history comes from ESPN's fantasy API: every player-season's actual and projected
          scoring, every draft pick, final standings, and consensus market ranking (ADP) from the
          seasons it's on record for. Sizing it matters — the same correlation reads very
          differently against 14 teams in one season than against thousands of player-seasons.
        </>
      }
    >
      <Table head={["Table", "What it holds", "Seasons", "Rows"]} align={["left", "left", "left", "right"]}>
        {tables.map((t) => (
          <tr key={t.table} className="border-t border-line/60">
            <td className="py-2 pr-3 font-medium text-ink">{t.label}</td>
            <td className="py-2 pr-3 text-xs text-ink-faint">{t.blurb}</td>
            <td className="tabular py-2 pr-3 text-xs text-ink-muted">{t.years || "—"}</td>
            <td className="tabular py-2 text-right font-semibold text-ink-muted">
              {t.rows.toLocaleString()}
            </td>
          </tr>
        ))}
      </Table>

      <Sub>Two data problems worth knowing about</Sub>
      <div className="grid gap-4 sm:grid-cols-2">
        <div className="rounded-lg border border-line/60 bg-surface-panel p-3.5">
          <div className="mb-1 flex items-center gap-1.5 text-xs font-semibold text-amber-300">
            <AlertTriangle size={12} /> The position column wasn't position
          </div>
          <p className="text-xs leading-relaxed text-ink-muted">
            ESPN exposes both <code className="text-ink-muted">lineupSlot</code> (where a player sat
            that week — "BE" for bench, "RB/WR/TE" for a flex start) and{" "}
            <code className="text-ink-muted">eligibleSlots</code> (every slot they're allowed to
            fill, which is a property of the player). This project originally recorded the former as{" "}
            <code className="text-ink-muted">position</code>, so about 76% of every season arrived
            labeled "BE" or "0" and was silently dropped by every downstream position filter — and
            the quarter that survived was biased toward players good enough to hold a named starting
            slot. Deriving from <code className="text-ink-muted">eligibleSlots</code> recovers 99.97%
            of rows and agrees with the old labels wherever they were already right. It roughly
            quadrupled every sample on this page.
          </p>
        </div>
        <div className="rounded-lg border border-line/60 bg-surface-panel p-3.5">
          <div className="mb-1 flex items-center gap-1.5 text-xs font-semibold text-amber-300">
            <AlertTriangle size={12} /> {broken.join(", ") || "2023"} is excluded
          </div>
          <p className="text-xs leading-relaxed text-ink-muted">
            That season has a genuine extraction gap: projected points were recorded as 0.0 for 392
            of 480 players. Patrick Mahomes shows 267 actual points against a projection of zero —
            these aren't legitimate zero projections, they're missing data. Filtering them leaves 88
            rows that aren't a random sample of the season, so reporting accuracy from them would be
            reporting an artefact of which rows happened to survive. Every accuracy figure below
            omits it entirely rather than quietly averaging it in.
          </p>
        </div>
      </div>
      <Note>
        One more that only bites at draft time: ADP for an upcoming season isn't published until
        shortly before the draft, so the live tool falls back to the most recent season on record
        rather than treating every player as having no market data. The board reports which season
        its timing signal came from.
      </Note>
    </Section>
  );
}

// --- 2. replacement level ------------------------------------------------

function ReplacementSection({ data }) {
  const rl = data.replacement_levels || {};
  const rows = rl.positions || [];
  const needs = Object.entries(data.roster_needs || {});
  return (
    <Section
      id="vorp"
      icon={<Ruler size={17} />}
      eyebrow="NB03 — the measuring stick"
      title={`Replacement level: what VORP is actually over, ${data.year}`}
      blurb={
        <>
          Every number on the draft board is a value <em>over replacement</em>, so the whole thing
          rests on what counts as replacement. A player's worth isn't their points — it's how many
          more they score than the player you'd otherwise have had for free. 300 points from a
          quarterback and 300 from a tight end are not remotely the same thing.
        </>
      }
    >
      <p className="mb-4 max-w-3xl text-sm leading-relaxed text-ink-muted">
        Replacement is defined as the last player at a position who'd still be starting somewhere in
        the league: the <strong className="text-ink-muted">Nth best</strong>, where N = teams ×
        starters at that position, plus that position's share of the FLEX slot. For this league
        that's {rl.teams ?? data.teams_in_league} teams starting{" "}
        {needs.map(([k, v]) => `${v} ${k}`).join(", ")}.
      </p>

      {rows.length > 0 && (
        <Table
          head={["Pos", "Startable league-wide", "Replacement level", "Best that season", "Pool"]}
          align={["left", "right", "right", "right", "right"]}
        >
          {rows.map((r) => (
            <tr key={r.position} className="border-t border-line/60">
              <td className="py-2 pr-3"><PositionChip position={r.position} /></td>
              <td className="tabular py-2 pr-3 text-right text-ink-muted">{r.starters_league_wide}</td>
              <td className="tabular py-2 pr-3 text-right font-semibold text-ink">
                {fmt(r.baseline_points, 0)}
              </td>
              <td className="tabular py-2 pr-3 text-right text-ink-muted">{fmt(r.best_points, 0)}</td>
              <td className="tabular py-2 text-right text-ink-ghost">{r.pool}</td>
            </tr>
          ))}
        </Table>
      )}

      <Verdict tone="info">
        Read the gap between the last two columns, not the columns themselves. A position where the
        best player towers over replacement is where a pick buys you the most; one where they're
        close is a position you can wait on. That gap is exactly what VORP measures, and it's why
        the board will happily tell you to pass on a high-scoring kicker.
      </Verdict>

      <Note>
        This replaced hand-picked rank cutoffs (“QB ranks 15–18”, “RB/WR 29–32”) that were guessed to
        roughly fit a 14-team league, weren't derived from the actual roster settings, and disagreed
        with the three other places the codebase separately computed VORP. One of those branches had
        a real bug: it returned the replacement player's raw points instead of{" "}
        <code className="text-ink-muted">points − replacement</code>, making QB and RB "VORP" a
        constant. There is now one implementation, in <code className="text-ink-muted">src/scoring.py</code>,
        shared by every notebook and by the live API.
      </Note>
    </Section>
  );
}

// --- 3. did drafting matter ----------------------------------------------

/** A section that has nothing to say yet, saying so instead of showing blanks. */
const NeedsHistory = ({ id, icon, eyebrow, title, what }) => (
  <Section id={id} icon={icon} eyebrow={eyebrow} title={title}
           blurb={<>Needs completed drafts from prior seasons, which this league doesn't have yet.
                  Once it has played a season, this section {what}.</>} />
);

function DraftPerformanceSection({ data, leagueName, myTeamName }) {
  const dp = data.draft_performance || {};
  const corr = dp.correlation;
  const verdict = correlationVerdict(corr);
  const teams = dp.teams || [];
  const maxVorp = Math.max(...teams.map((t) => Math.abs(t.avg_vorp || 0)), 1);

  // The shareable version of this section. Same numbers, composed for a group
  // chat: the league and season named, the reader's own team marked, and the
  // correlation stated in words underneath so the ranking isn't quoted without
  // the caveat that comes with it.
  const isMine = (t) => !!myTeamName && t.team_name === myTeamName;
  const league = leagueName || "This league";
  const shareTitle = `Who drafted best — ${data.year}`;
  const shareFooter =
    corr?.r != null
      ? `Drafting well tracked finishing well: r = ${fmt(corr.r, 2)} across ${corr.n} teams.`
      : null;
  const shareNote = "Average VORP of every player drafted, scored on what they actually went on to do.";

  const shareCard = () =>
    drawRankedCard({
      eyebrow: league,
      title: shareTitle,
      rows: teams.map((t) => ({
        label: t.team_name,
        sub: `finished ${ordinal(t.final_standing)}`,
        value: t.avg_vorp,
        valueText: `${t.avg_vorp > 0 ? "+" : ""}${fmt(t.avg_vorp, 1)}`,
        highlight: isMine(t),
      })),
      footer: shareFooter,
      note: shareNote,
    });

  const shareText = [
    `${league} — who drafted best, ${data.year}`,
    "",
    ...teams.map((t, i) =>
      `${String(i + 1).padStart(2)}. ${t.team_name}${isMine(t) ? " (me)" : ""} — ` +
      `${t.avg_vorp > 0 ? "+" : ""}${fmt(t.avg_vorp, 1)} avg VORP, finished ${ordinal(t.final_standing)}`
    ),
    "",
    shareFooter,
    shareNote,
    "via Justin's Draft Assistant",
  ].filter(Boolean).join("\n");

  // A blank chart under "did drafting matter?" reads as "no" rather than as
  // "not measurable here", which is the opposite of what's true.
  if (!teams.length) {
    return (
      <NeedsHistory
        id="draft" icon={<Crown size={17} />} eyebrow="NB03 — the premise"
        title="Did drafting well actually matter?"
        what="grades every manager's draft on the same VORP scale the board ranks with, and
              checks it against where they finished"
      />
    );
  }

  return (
    <Section
      id="draft"
      icon={<Crown size={17} />}
      eyebrow="NB03 — the premise"
      title={`Did drafting well actually matter? ${data.year}`}
      action={<ShareButton draw={shareCard} text={shareText}
                           filename={`who-drafted-best-${data.year}`} />}
      blurb={
        <>
          This is the question the entire tool rests on. If the draft didn't predict the season,
          then a better draft board is a solution to nothing. Each team's drafted players are scored
          on the same VORP scale the live board ranks with, averaged, and set against where that
          team finished.
        </>
      }
    >
      {corr?.r != null && (
        <>
          <Figures>
            <Figure
              value={fmt(corr.r, 2)}
              label="Pearson r — draft VORP vs. final standing"
              tone={Math.abs(corr.r) >= 0.4 ? "text-emerald-400" : "text-ink"}
            />
            <Figure
              value={corr.p < 0.001 ? "< 0.001" : fmt(corr.p, 3)}
              label={verdict?.significant ? "p-value — statistically significant" : "p-value — not significant"}
            />
            <Figure value={corr.n} label="teams in the sample" />
          </Figures>
          {/* Now that any season can be selected, the null result is a real
              case and not a hypothetical: McFL's 2020 draft has r = -0.05 at
              p = 0.87. Reporting the sign of that as "drafting better went
              with finishing higher" would be reading a direction out of noise,
              which is exactly the mistake this page exists to argue against. */}
          {verdict.significant ? (
            <Verdict>
              A {verdict.strength}, statistically significant relationship. The sign is negative
              because 1st is the best finish, so drafting better went with finishing higher.{" "}
              {corr.n <= 14 && (
                <span className="opacity-70">
                  Hold it loosely all the same: one season of {corr.n} teams is a small sample, and
                  this is a correlation, not a controlled experiment — good drafters likely also
                  manage their waiver wire well.
                </span>
              )}
            </Verdict>
          ) : (
            <Verdict tone="warn">
              <strong>No relationship worth reading in this season.</strong> At p ={" "}
              {corr.p < 0.001 ? "< 0.001" : fmt(corr.p, 3)}, a correlation this size is what {corr.n}{" "}
              teams produce by chance, so the sign of it means nothing — the best-drafting team here
              is not evidence that drafting well paid off. Seasons vary, and a single one of{" "}
              {corr.n} teams has little power to detect anything; check the other years before
              concluding the draft doesn't matter in this league.
            </Verdict>
          )}
        </>
      )}

      <Sub>Every team, drafted value against finish</Sub>
      <div className="space-y-2">
        {teams.map((t, i) => (
          <div key={t.team_name} className="grid grid-cols-[1.5rem_minmax(0,1fr)_4.5rem] items-center gap-3">
            <span className="tabular text-xs text-ink-ghost">{i + 1}</span>
            <div className="min-w-0">
              <div className="mb-1 flex items-baseline justify-between gap-2">
                <span className="truncate text-sm text-ink-muted">{t.team_name}</span>
                <span className="tabular shrink-0 text-xs text-ink-faint">
                  {fmt(t.avg_vorp, 1)} avg VORP
                </span>
              </div>
              <DivergingBar value={t.avg_vorp} max={maxVorp} />
            </div>
            <span
              className={`tabular text-right text-xs font-semibold ${
                t.final_standing <= 3 ? "text-emerald-400" : "text-ink-faint"
              }`}
            >
              {ordinal(t.final_standing)}
            </span>
          </div>
        ))}
      </div>
      <Note>
        The exceptions are the interesting part. A team can draft near the bottom and still win —
        which is the honest reminder that r = {fmt(corr?.r, 2)} is a tendency across a league, not a
        promise about any one team's season.
      </Note>
    </Section>
  );
}

// --- 4. draft capital ----------------------------------------------------

function RoundsSection({ data }) {
  const rounds = data.draft_value_by_round || [];
  if (!rounds.length) return null;
  const max = Math.max(...rounds.map((r) => Math.abs(r.avg_vorp || 0)), 1);
  return (
    <Section
      id="rounds"
      icon={<Layers size={17} />}
      eyebrow="NB03 — where value lives"
      title={`How fast draft capital decays, ${data.year}`}
      blurb={
        <>
          Average VORP returned by each round, with the share of picks in that round that beat
          replacement at all. This is what makes the first two rounds worth agonising over and the
          back half worth treating as lottery tickets.
        </>
      }
    >
      <div className="space-y-2">
        {rounds.map((r) => (
          <div key={r.round} className="grid grid-cols-[2.5rem_minmax(0,1fr)_7rem] items-center gap-3">
            <span className="tabular text-xs text-ink-faint">R{r.round}</span>
            <DivergingBar value={r.avg_vorp} max={max} />
            <span className="tabular text-right text-xs text-ink-muted">
              {fmt(r.avg_vorp, 0)}
              <span className="ml-2 text-ink-ghost">{pctOf(r.hit_rate)} hit</span>
            </span>
          </div>
        ))}
      </div>
      <Note>
        "Hit" means the pick returned more than replacement level — i.e. it was worth a roster spot
        at all. The decline isn't smooth, and the noise in later rounds is real: with roughly{" "}
        {rounds[0]?.picks ?? 14} picks per round in a single season, one outlier moves a round's
        average substantially.
      </Note>
    </Section>
  );
}

// --- 4b. how this league is odd -----------------------------------------

/** Per-season means as a row of signed bars. Consistency is the argument. */
function Sparkline({ rows, max }) {
  if (!rows?.length) return <span className="text-xs text-ink-ghost">—</span>;
  return (
    <div className="flex items-end gap-0.5" title={rows.map((r) => `${r.year}: ${fmt(r.mean, 1)}`).join("  ")}>
      {rows.map((r) => {
        const h = Math.min(Math.abs(r.mean) / (max || 1), 1) * 100;
        const up = r.mean >= 0;
        return (
          <div key={r.year} className="relative h-6 w-2.5">
            <div className="absolute inset-x-0 top-1/2 h-px bg-surface-hover" />
            <div
              className={`absolute inset-x-0 rounded-sm ${up ? "bg-emerald-400/80 bottom-1/2" : "bg-rose-400/80 top-1/2"}`}
              style={{ height: `${Math.max(h / 2, 3)}%` }}
            />
          </div>
        );
      })}
    </div>
  );
}

function LeagueBiasSection({ data }) {
  const lb = data.league_bias || {};
  const positions = lb.position || [];
  // The stored fit belongs to whichever league was last processed. Showing it
  // here for a different league would attribute one set of drafters' habits to
  // another - the exact mistake this section exists to correct.
  const fittedElsewhere =
    lb.meta?.league_id && data.league_id && String(lb.meta.league_id) !== String(data.league_id);
  if (!positions.length || fittedElsewhere) {
    return (
      <Section
        id="bias"
        icon={<Fingerprint size={17} />}
        eyebrow="Live tool — league fingerprint"
        title="How this league drafts differently"
        blurb={
          fittedElsewhere
            ? "Not measured for this league. A draft-habit fit needs several hundred of this " +
              "league's own picks against the market; the stored fit belongs to another league " +
              "and says nothing about the people you draft against here. The board falls back " +
              "to market ADP timing, which it says on the board itself."
            : "Not measured yet. Run notebooks/compute_league_bias.py to fit it."
        }
      />
    );
  }

  const meta = lb.meta || {};
  const posSeason = lb.position_season || [];
  const teamSeason = lb.proteam_season || [];
  const seasonsFor = (rows, key, val) => rows.filter((r) => r[key] === val);
  const maxPos = Math.max(...posSeason.map((r) => Math.abs(r.mean || 0)), 1);

  // Only NFL teams the fit actually moves. The other ~28 are inside the noise,
  // and a sorted 32-row table would invite meaning to be read into them.
  const teams = (lb.pro_team || [])
    .filter((t) => Math.abs(t.shrunk || 0) >= 2)
    .sort((a, b) => Math.abs(b.shrunk) - Math.abs(a.shrunk));
  const topTeam = teams[0];

  // Expansion franchises have half the history; excluded on `seasons_n` rather
  // than by hardcoding which ids joined in 2024.
  const fullSeasons = Math.max(...(lb.manager || []).map((m) => m.seasons_n || 0), 0);
  const managers = (lb.manager || [])
    .filter((m) => (m.seasons_n || 0) >= fullSeasons && Math.abs(m.shrunk || 0) >= 2)
    .sort((a, b) => a.shrunk - b.shrunk);

  const strictPlayers = (lb.player || [])
    .filter((p) => p.passes_strict_filter)
    .sort((a, b) => Math.abs(b.mean) - Math.abs(a.mean))
    .slice(0, 8);

  return (
    <Section
      id="bias"
      icon={<Fingerprint size={17} />}
      eyebrow="Live tool — league fingerprint"
      title="How this league drafts differently from the market"
      blurb={
        <>
          Every player carries a national average draft position, but a league drafts to its own
          habits. Comparing {meta.n_picks ?? "~1,000"} of this league's picks against what the market
          said, across {meta.years?.split(",").length ?? 6} seasons, shows where those habits are
          real — and where they only look real.
        </>
      }
    >
      <Figures cols={4}>
        <Figure value={meta.n_picks ?? "—"} label="picks with a market price" />
        <Figure value={fmt(meta.resid_sd, 1)} label="typical gap from ADP, in picks" />
        <Figure value={meta.years?.replace(/,/g, " ") ?? "—"} label="seasons measured" tone="text-ink-muted" />
        <Figure value={`≤ ${fmt(meta.adp_cutoff, 0)}`} label="ADP cutoff (board length)" tone="text-ink-muted" />
      </Figures>

      <Verdict tone="info">
        Most of that {fmt(meta.resid_sd, 0)}-pick spread is just draft-day randomness. The sections
        below are the part that repeats — and only the position and NFL-team effects are actually
        applied to the board. Everything is a <strong>timing</strong> adjustment: it moves when a
        player is expected to be gone, never what they're worth.
      </Verdict>

      <Sub>By position — applied</Sub>
      <p className="mb-3 max-w-3xl text-xs leading-relaxed text-ink-faint">
        All six are shown, including the ones with no effect — hiding the null rows is how a table
        like this turns into a leaderboard. <strong className="text-ink-muted">Shrunk</strong> is the
        number the board actually uses: each estimate is pulled toward zero in proportion to how
        little data supports it.
      </p>
      <Table
        head={["Pos", "n", "Raw mean", "t", "Applied", "Per season"]}
        align={["left", "right", "right", "right", "right", "left"]}
      >
        {positions.map((p) => (
          <tr key={p.position} className="border-t border-line/60">
            <td className="py-2 pr-3"><PositionChip position={p.position} /></td>
            <td className="tabular py-2 pr-3 text-right text-ink-ghost">{p.n}</td>
            <td className="tabular py-2 pr-3 text-right text-ink-muted">{fmt(p.mean, 1)}</td>
            <td className={`tabular py-2 pr-3 text-right ${Math.abs(p.t) >= 2 ? "text-ink-muted" : "text-ink-ghost"}`}>
              {fmt(p.t, 2)}
            </td>
            <td className={`tabular py-2 pr-3 text-right font-semibold ${
              Math.abs(p.shrunk) < 2 ? "text-ink-ghost"
                : p.shrunk < 0 ? "text-rose-300" : "text-emerald-300"}`}>
              {p.shrunk > 0 ? "+" : ""}{fmt(p.shrunk, 1)}
            </td>
            <td className="py-2">
              <Sparkline rows={seasonsFor(posSeason, "position", p.position)} max={maxPos} />
            </td>
          </tr>
        ))}
      </Table>
      <Verdict tone="warn">
        <strong>Quarterbacks are the finding.</strong> This league takes them about a full round
        ahead of the market, and the sparkline is why that counts: every season, same direction, no
        exceptions. Tight ends lean the same way more mildly. Kickers and defenses last longer here
        than the market expects. Running backs and receivers show nothing at all — on the skill
        positions, this league and the market agree.
      </Verdict>

      {topTeam && (
        <>
          <Sub>By NFL team — applied</Sub>
          <p className="mb-3 max-w-3xl text-xs leading-relaxed text-ink-faint">
            Measured after removing each season's position effects, so this isn't just "they draft a
            lot of quarterbacks". Only teams the fit still moves by two or more picks are listed; the
            remaining {32 - teams.length} are inside the noise and a ranked table of them would
            manufacture meaning that isn't there.
          </p>
          <Table
            head={["Team", "n", "Raw mean", "t", "Applied", "Per season"]}
            align={["left", "right", "right", "right", "right", "left"]}
          >
            {teams.map((t) => (
              <tr key={t.pro_team} className="border-t border-line/60">
                <td className="py-2 pr-3 font-medium text-ink">{t.pro_team}</td>
                <td className="tabular py-2 pr-3 text-right text-ink-ghost">{t.n}</td>
                <td className="tabular py-2 pr-3 text-right text-ink-muted">{fmt(t.mean, 1)}</td>
                <td className={`tabular py-2 pr-3 text-right ${Math.abs(t.t) >= 2 ? "text-ink-muted" : "text-ink-ghost"}`}>
                  {fmt(t.t, 2)}
                </td>
                <td className={`tabular py-2 pr-3 text-right font-semibold ${t.shrunk < 0 ? "text-rose-300" : "text-emerald-300"}`}>
                  {t.shrunk > 0 ? "+" : ""}{fmt(t.shrunk, 1)}
                </td>
                <td className="py-2">
                  <Sparkline rows={seasonsFor(teamSeason, "pro_team", t.pro_team)} max={maxPos} />
                </td>
              </tr>
            ))}
          </Table>
          <Verdict>
            <strong>{topTeam.pro_team} is the strongest single habit in the league.</strong> Their
            players go about {fmt(Math.abs(topTeam.mean), 0)} picks ahead of the market on raw
            average — applied as {fmt(Math.abs(topTeam.shrunk), 0)} after shrinkage, because with
            32 NFL teams examined at once the largest of them is expected to look extreme by
            chance. It survives that test{topTeam.p_perm != null && ` (family-wise p = ${fmt(topTeam.p_perm, 4)})`},
            and it isn't one enthusiastic manager — it shows up across most of the league.
          </Verdict>
        </>
      )}

      {managers.length > 0 && (
        <>
          <Sub>By manager — measured, not applied</Sub>
          <p className="mb-3 max-w-3xl text-xs leading-relaxed text-ink-faint">
            Some managers reliably reach; others reliably wait. Real, but deliberately{" "}
            <strong className="text-ink-muted">not</strong> applied to the board: this effect belongs
            to a drafter, not a player, and the tool has no idea which of your league-mates is on the
            clock at any given pick. Franchises with fewer than {fullSeasons} seasons are excluded.
          </p>
          <Table head={["Manager", "n", "Mean", "t", "Shrunk"]} align={["left", "right", "right", "right", "right"]}>
            {managers.map((m) => (
              <tr key={m.team_id} className="border-t border-line/60">
                <td className="py-2 pr-3 text-ink-muted">{m.team_name || `Team ${m.team_id}`}</td>
                <td className="tabular py-2 pr-3 text-right text-ink-ghost">{m.n}</td>
                <td className="tabular py-2 pr-3 text-right text-ink-muted">{fmt(m.mean, 1)}</td>
                <td className="tabular py-2 pr-3 text-right text-ink-faint">{fmt(m.t, 2)}</td>
                <td className={`tabular py-2 text-right font-semibold ${m.shrunk < 0 ? "text-rose-300" : "text-emerald-300"}`}>
                  {m.shrunk > 0 ? "+" : ""}{fmt(m.shrunk, 1)}
                </td>
              </tr>
            ))}
          </Table>
          <Note>
            Negative means they reach; positive means they wait. Grouping is on the franchise id, not
            the team name — four teams renamed themselves, and two of the names differ by a single
            space: "Gerald Pea's Football Team" and "GeraldPea's Football Team" are two{" "}
            <em>different</em> franchises. Keying on names would have merged two managers and split
            two others.
          </Note>
        </>
      )}

      {strictPlayers.length > 0 && (
        <>
          <Sub>By player — measured, not applied</Sub>
          <p className="mb-3 max-w-3xl text-xs leading-relaxed text-ink-faint">
            Players drafted at least three times, always in the same direction, with a spread smaller
            than the effect itself. {strictPlayers.length > 0 && `${(lb.player || []).filter((p) => p.passes_strict_filter).length} of ${(lb.player || []).length} players clear that bar.`}
          </p>
          <Table head={["Player", "Seasons", "Mean", "Spread"]} align={["left", "right", "right", "right"]}>
            {strictPlayers.map((p) => (
              <tr key={p.player_id} className="border-t border-line/60">
                <td className="py-2 pr-3 text-ink-muted">{p.player_name}</td>
                <td className="tabular py-2 pr-3 text-right text-ink-ghost">{p.n}</td>
                <td className={`tabular py-2 pr-3 text-right font-semibold ${p.mean < 0 ? "text-rose-300" : "text-emerald-300"}`}>
                  {p.mean > 0 ? "+" : ""}{fmt(p.mean, 1)}
                </td>
                <td className="tabular py-2 text-right text-ink-ghost">±{fmt(p.sd, 1)}</td>
              </tr>
            ))}
          </Table>
          <Verdict tone="warn">
            <strong>None of this is applied to the board, and it shouldn't be.</strong> Three or four
            observations against a typical gap of {fmt(meta.resid_sd, 0)} picks is not enough to call
            a preference, and the filter that selected these names was run on the same data that
            produced them — which is exactly how you manufacture a pattern. Read it as trivia about
            past drafts, not as a prediction.
          </Verdict>
        </>
      )}
    </Section>
  );
}

// --- 5. steals and reaches ----------------------------------------------

const PlayerTable = ({ rows, deltaLabel }) =>
  !rows?.length ? (
    <div className="text-xs text-ink-faint">No data.</div>
  ) : (
    <Table
      head={["Player", "Pos", "Pick", "ADP", deltaLabel, "VORP"]}
      align={["left", "left", "right", "right", "right", "right"]}
    >
      {rows.map((r) => (
        <tr key={`${r.player_name}-${r.pick}`} className="border-t border-line/60">
          <td className="py-2 pr-3">
            <div className="font-medium text-ink">{r.player_name}</div>
            <div className="text-[11px] text-ink-ghost">{r.team_name}</div>
          </td>
          <td className="py-2 pr-3"><PositionChip position={r.position} /></td>
          <td className="tabular py-2 pr-3 text-right text-ink-muted">{r.pick}</td>
          <td className="tabular py-2 pr-3 text-right text-ink-muted">{fmtAdp(r.adp)}</td>
          <td className="tabular py-2 pr-3 text-right text-ink-muted">
            {r.draft_delta > 0 ? "+" : ""}{fmt(r.draft_delta, 0)}
          </td>
          <td className={`tabular py-2 text-right font-semibold ${r.vorp >= 0 ? "text-emerald-400" : "text-rose-400"}`}>
            {fmt(r.vorp, 0)}
          </td>
        </tr>
      ))}
    </Table>
  );

function MarketSection({ data, leagueName, myTeamName }) {
  const steals = data.steals_and_reaches?.steals || [];
  const reaches = data.steals_and_reaches?.reaches || [];

  if (!steals.length && !reaches.length) {
    return (
      <NeedsHistory
        id="market" icon={<Gem size={17} />} eyebrow="NB03 — where the crowd was wrong"
        title="Steals and reaches"
        what="lists the picks that returned far more or far less than their draft slot was worth"
      />
    );
  }

  // Shared as the steals alone: "look what I got in the ninth round" travels,
  // and a chat message listing everyone's worst picks alongside is a different
  // and less welcome thing to send.
  const league = leagueName || "This league";
  const top = steals.slice(0, 8);
  const shareCard = () =>
    drawRankedCard({
      eyebrow: `${league} · ${data.year}`,
      title: "Best value of the draft",
      rows: top.map((p) => ({
        label: p.player_name,
        sub: `${p.position} · pick ${p.pick} · ${p.team_name}`,
        value: p.vorp,
        valueText: `${fmt(p.vorp, 0)} VORP`,
        highlight: !!myTeamName && p.team_name === myTeamName,
      })),
      footer: "Value over replacement, against where the market said they'd go.",
      note: "Steals are late picks that paid off — the thing the board is trying to find a season early.",
    });

  const shareText = [
    `${league} — best value of the ${data.year} draft`,
    "",
    ...top.map((p, i) =>
      `${String(i + 1).padStart(2)}. ${p.player_name} (${p.position}) — pick ${p.pick}, ` +
      `ADP ${fmtAdp(p.adp)}, ${fmt(p.vorp, 0)} VORP — ${p.team_name}` +
      (myTeamName && p.team_name === myTeamName ? " (me)" : "")
    ),
    "",
    "via Justin's Draft Assistant",
  ].join("\n");

  return (
  <Section
    id="market"
    icon={<Gem size={17} />}
    eyebrow="NB03 — where the crowd was wrong"
    title={`Steals and reaches, ${data.year}`}
    action={<ShareButton draw={shareCard} text={shareText}
                         filename={`best-value-${data.year}`} />}
    blurb={
      <>
        Draft delta is pick number minus ADP, so a positive number means a player lasted longer than
        the market expected. Steals are late picks that paid off; reaches are early picks that
        didn't. Finding the first kind one season <em>early</em> is what the board is trying to do.
      </>
    }
  >
    <Sub>Fell past their ADP and delivered anyway</Sub>
    <PlayerTable rows={steals} deltaLabel="Fell by" />
    <Sub>Taken ahead of ADP and returned the least</Sub>
    <PlayerTable rows={reaches} deltaLabel="Rose by" />
    <Note>
      Reaching early is the expensive mistake — you pay a premium <em>and</em> forfeit the player you
      could have had. That asymmetry is what the board's pick-timing multiplier exists to manage: it
      estimates whether someone will survive to your next turn instead of guessing.
    </Note>
  </Section>
  );
}

// --- 6. projection accuracy ---------------------------------------------

function AccuracySection({ data }) {
  const pa = data.projection_accuracy || {};
  const overall = pa.overall;
  const seasons = pa.by_season || [];
  if (!overall) return null;
  return (
    <Section
      id="accuracy"
      icon={<Activity size={17} />}
      eyebrow="NB05 — grading the forecast"
      title="How accurate are the projections, really?"
      blurb={
        <>
          The board leans on ESPN's preseason projections, so their accuracy is the ceiling on
          everything it can do. Both the projection and the outcome are converted to VORP first, per
          season, so they're on one comparable scale — replacement level shifts year to year, and
          grading raw points would confuse a high-scoring season with an accurate forecast.
        </>
      }
    >
      <Figures cols={4}>
        <Figure value={fmt(overall.r2, 2)} label="R² — variance in outcomes explained" tone="text-sky-400" />
        <Figure value={fmt(overall.rmse, 0)} label="RMSE — typical miss, in VORP" />
        <Figure value={fmt(overall.bias, 1)} label="Mean signed error — projection bias" />
        <Figure value={overall.n?.toLocaleString()} label="player-seasons graded" />
      </Figures>

      <Sub>Season by season</Sub>
      <div className="space-y-2">
        {seasons.map((s) => (
          <div key={s.year} className="grid grid-cols-[3rem_minmax(0,1fr)_9rem] items-center gap-3">
            <span className="tabular text-xs text-ink-faint">{s.year}</span>
            <ShareBar value={Math.max(0, s.r2 ?? 0)} />
            <span className="tabular text-right text-xs text-ink-muted">
              R² {fmt(s.r2, 2)}
              <span className="ml-2 text-ink-ghost">±{fmt(s.rmse, 0)}</span>
            </span>
          </div>
        ))}
      </div>

      <Verdict tone={overall.bias < -3 ? "warn" : "info"}>
        Roughly {pctOf(overall.r2)} of the variance in what players actually delivered is explained
        by their preseason projection — genuinely useful, and far from decisive. The mean signed
        error of {fmt(overall.bias, 1)} says projections ran{" "}
        {overall.bias < 0 ? "low" : "high"} on average: players{" "}
        {overall.bias < 0 ? "out-delivered" : "fell short of"} their numbers by about{" "}
        {fmt(Math.abs(overall.bias), 0)} VORP. Year-to-year spread matters too — the best and worst
        seasons here differ by a wide margin, so a single season's accuracy is not a stable estimate.
      </Verdict>
      {pa.excluded?.length > 0 && (
        <Note>{pa.excluded.join(", ")} excluded — see the data section for why.</Note>
      )}
    </Section>
  );
}

// --- 7. reliability by position -----------------------------------------

function PositionReliabilitySection({ data }) {
  const rows = data.position_reliability || [];
  if (!rows.length) return null;
  return (
    <Section
      id="positions"
      icon={<ShieldCheck size={17} />}
      eyebrow="NB05 — the confidence column"
      title="Some positions are far more forecastable than others"
      blurb={
        <>
          The same accuracy question, split by position and measured across every season on record.
          This is the larger half of the board's Confidence column, and the single most decision-
          relevant table on this page.
        </>
      }
    >
      <div className="space-y-2.5">
        {rows.map((p) => (
          <div key={p.position} className="grid grid-cols-[3rem_minmax(0,1fr)_9rem] items-center gap-3">
            <PositionChip position={p.position} />
            <ShareBar
              value={Math.max(0, p.r2 ?? 0)}
              tone={(p.r2 ?? 0) <= 0.05 ? "bg-rose-400" : "bg-sky-400"}
            />
            <span className="tabular text-right text-xs text-ink-muted">
              R² {fmt(p.r2, 2)}
              <span className="ml-2 text-ink-ghost">±{fmt(p.rmse, 0)}</span>
            </span>
          </div>
        ))}
      </div>
      <Verdict tone="warn">
        Kickers and defenses are the finding here. A negative R² means the projection did{" "}
        <em>worse than guessing the average every time</em> — there is no skill in the forecast at
        all. No matter how good a kicker's projected total looks, it carries essentially no
        information, which is why the board discounts them heavily and why spending an early pick
        there is indefensible.
      </Verdict>
      <Note>
        The trailing figure is RMSE, the typical miss in VORP. Note that it doesn't track R²:
        kickers have the second-smallest absolute error but no explanatory power, because kicker
        outcomes barely vary in the first place. Small errors on a narrow distribution isn't skill —
        it's a position where everyone scores about the same and picking one is close to a coin flip.
      </Note>
    </Section>
  );
}

// --- 8. rookies ----------------------------------------------------------

function RookieSection({ data }) {
  const rookie = (data.rookie_reliability || []).find((r) => r.group === "rookie");
  const vet = (data.rookie_reliability || []).find((r) => r.group === "veteran");
  if (!rookie || !vet) return null;
  return (
    <Section
      id="rookies"
      icon={<BarChart3 size={17} />}
      eyebrow="NB05 — unproven players"
      title="Less predictable, but not worse"
      blurb={
        <>
          A player with no prior season on record has nothing behind their projection but the
          projection itself. Measuring the two groups separately shows what that costs — and turns up
          something that runs against the usual draft-day instinct.
        </>
      }
    >
      <Figures cols={4}>
        <Figure value={fmt(vet.r2, 2)} label="R² — veterans" />
        <Figure value={fmt(rookie.r2, 2)} label="R² — unproven" tone="text-amber-400" />
        <Figure
          value={pctOf(rookie.reliability_factor)}
          label="what an unproven projection is worth"
          tone="text-amber-400"
        />
        <Figure
          value={`+${fmt(Math.abs(rookie.mean_signed_error), 0)}`}
          label="VORP they beat their projection by"
          tone="text-emerald-400"
        />
      </Figures>
      <Verdict tone="warn">
        Unproven players out-delivered their projections by{" "}
        {fmt(Math.abs(rookie.mean_signed_error), 0)} VORP on average, against{" "}
        {fmt(Math.abs(vet.mean_signed_error), 0)} for veterans — roughly three times as much. They
        are <em>under</em>-projected, not over-projected. So the board discounts them purely for{" "}
        <em>uncertainty</em>, never for expected value: their outcomes are far more scattered. Set
        the risk dial to zero and an unproven player with the same projection as a veteran scores
        identically.
      </Verdict>
      <Note>
        The label reads "Unproven" rather than "Rookie" because that's what the flag actually
        measures — no prior-season production on record. A veteran who missed last season looks
        identical to the model, and calling them a rookie on the board would simply be wrong.
        Position and unproven status compose multiplicatively, so an unproven tight end and an
        unproven running back don't collapse to the same number.
      </Note>
    </Section>
  );
}

// --- 9. beating the market ----------------------------------------------

function BenchmarkSection({ data }) {
  const scopes = data.adp_benchmark?.scopes || [];
  if (!scopes.length) return null;
  return (
    <Section
      id="benchmark"
      icon={<Target size={17} />}
      eyebrow="NB05 — the benchmark that matters"
      title="Do the projections beat the market?"
      blurb={
        <>
          Every accuracy figure so far answers "how wrong were the projections". None answer the
          question a drafter actually faces, which is comparative:{" "}
          <em>would I do better ignoring them and just drafting off ADP?</em> The crowd's ranking is
          the free alternative to any model, so it's the benchmark to beat.
        </>
      }
    >
      <p className="mb-4 max-w-3xl text-sm leading-relaxed text-ink-muted">
        Compared by <strong className="text-ink-muted">Spearman rank correlation</strong>, not error:
        ADP is a pick number and VORP is points above replacement, so an error metric between them is
        meaningless. Drafting is an ordering problem anyway — you never need to know a player will
        score 214.7, only that he should go before the next guy on your board.
      </p>

      <div className="space-y-4">
        {scopes.map((s) => {
          const won = s.mean_projection > s.mean_market;
          return (
            <div key={s.scope} className="rounded-lg border border-line/60 bg-surface-panel p-3.5">
              <div className="mb-2.5 flex flex-wrap items-baseline justify-between gap-2">
                <span className="text-sm font-semibold text-ink">{s.scope}</span>
                <span className="text-xs text-ink-faint">
                  projection won {s.seasons_won} of {s.seasons} seasons
                </span>
              </div>
              <div className="space-y-2">
                <RankerBar label="Projection" value={s.mean_projection} tone="bg-emerald-400" />
                <RankerBar label="Market (ADP)" value={s.mean_market} tone="bg-ink-faint" />
              </div>
              <div className={`mt-2 text-[11px] ${won ? "text-emerald-300/80" : "text-amber-300/80"}`}>
                {won ? "Projection ahead by " : "Market ahead by "}
                {fmt(Math.abs(s.mean_projection - s.mean_market), 3)} mean Spearman
              </div>
            </div>
          );
        })}
      </div>

      <Verdict tone="warn">
        Across the full pool the projections beat the market clearly and in every season. But the
        full pool flatters both rankers — separating stars from deep-bench players is easy, and
        nobody agonises over it on draft day. Restricted to the top of the board, where picks
        actually get decided, the edge narrows sharply and the season-by-season record is close to a
        coin flip. <strong>The honest read: the projections are good, but not decisively better
        than the crowd where it counts.</strong>
      </Verdict>
      <Note>
        One caveat that cuts against reading too much into a narrow win either way: ADP is not
        independent of these projections. A large share of the people setting it are looking at the
        same numbers when they draft. These are correlated rankers, not a controlled comparison.
        Coverage is limited to seasons with both ADP and usable projections on record —{" "}
        {(data.adp_benchmark?.years || []).join(", ") || "a short window"} — which is a small sample
        from one league.
      </Note>
    </Section>
  );
}

const RankerBar = ({ label, value, tone }) => (
  <div className="grid grid-cols-[7rem_minmax(0,1fr)_3rem] items-center gap-3">
    <span className="text-xs text-ink-muted">{label}</span>
    <ShareBar value={value} tone={tone} />
    <span className="tabular text-right text-xs text-ink-muted">{fmt(value, 3)}</span>
  </div>
);

// --- 10. the model -------------------------------------------------------

function ModelSection({ data }) {
  const models = data.model_report || [];
  const ablation = data.model_ablation || [];
  const vif = data.feature_vif || [];
  if (!models.length && !ablation.length) return null;
  const selected = models.find((m) => m.selected === 1) || models[0];
  const base = ablation[0];
  const full = ablation[ablation.length - 1];

  return (
    <Section
      id="model"
      icon={<Activity size={17} />}
      eyebrow="NB04 — can we do better?"
      title="Trying to out-predict the projection (and failing)"
      blurb={
        <>
          The obvious next move is to train a model that improves on ESPN's projection using
          last season's production. This section is that attempt, done properly — and its
          conclusion is the reason the live tool works the way it does.
        </>
      }
    >
      <Sub>Method</Sub>
      <p className="max-w-3xl text-sm leading-relaxed text-ink-muted">
        A <strong className="text-ink-muted">walk-forward split</strong>: train on earlier seasons,
        hold out the most recent one entirely. Model selection uses{" "}
        <code className="text-ink-muted">GroupKFold</code> cross-validation grouped by season{" "}
        <em>within the training years only</em>, so a fold never trains and validates on the same
        season — a random shuffle would let information leak across folds and flatter every model.
        {selected && (
          <>
            {" "}Trained on {selected.train_seasons} ({selected.train_rows?.toLocaleString()} rows),
            tested on {selected.holdout_year} ({selected.test_rows?.toLocaleString()} rows).
          </>
        )}
      </p>

      {models.length > 0 && (
        <>
          <Sub>Model comparison (cross-validated on training seasons)</Sub>
          <Table head={["Model", "CV RMSE", "CV MAE", "CV R²"]} align={["left", "right", "right", "right"]}>
            {models.map((m) => (
              <tr key={m.model} className={`border-t border-line/60 ${m.selected ? "bg-emerald-500/[0.06]" : ""}`}>
                <td className="py-2 pr-3 font-medium text-ink">
                  {m.model}
                  {m.selected === 1 && (
                    <span className="ml-2 rounded bg-emerald-500/15 px-1.5 py-0.5 text-[10px] text-emerald-300">
                      selected
                    </span>
                  )}
                </td>
                <td className="tabular py-2 pr-3 text-right text-ink-muted">{fmt(m.cv_rmse, 2)}</td>
                <td className="tabular py-2 pr-3 text-right text-ink-muted">{fmt(m.cv_mae, 2)}</td>
                <td className="tabular py-2 text-right text-ink-muted">{fmt(m.cv_r2, 3)}</td>
              </tr>
            ))}
          </Table>
          {selected && (
            <div className="mt-3">
              <Figures>
                <Figure value={fmt(selected.holdout_r2, 3)} label={`Holdout R² (${selected.holdout_year}, never seen)`} tone="text-sky-400" />
                <Figure value={fmt(selected.holdout_rmse, 1)} label="Holdout RMSE, in VORP" />
                <Figure value={fmt(selected.holdout_mae, 1)} label="Holdout MAE, in VORP" />
              </Figures>
            </div>
          )}
        </>
      )}

      {ablation.length > 0 && (
        <>
          <Sub>Feature ablation — the actual test</Sub>
          <p className="mb-3 max-w-3xl text-sm leading-relaxed text-ink-muted">
            Same model, same split, adding one group of features at a time and reading the holdout
            score. This is the honest way to ask "would better features help, or is this the
            ceiling?" — if a group adds nothing on data the model never saw, it adds nothing,
            however plausible it looked going in.
          </p>
          <Table
            head={["Feature set", "n", "Holdout R²", "Δ R²", "RMSE"]}
            align={["left", "right", "right", "right", "right"]}
          >
            {ablation.map((a) => (
              <tr key={a.feature_set} className="border-t border-line/60">
                <td className="py-2 pr-3 text-ink-muted">{a.feature_set}</td>
                <td className="tabular py-2 pr-3 text-right text-ink-ghost">{a.n_features}</td>
                <td className="tabular py-2 pr-3 text-right font-semibold text-ink">
                  {fmt(a.holdout_r2, 3)}
                </td>
                <td
                  className={`tabular py-2 pr-3 text-right ${
                    (a.r2_gain ?? 0) > 0.001 ? "text-emerald-400"
                      : (a.r2_gain ?? 0) < -0.001 ? "text-rose-400" : "text-ink-ghost"
                  }`}
                >
                  {a.r2_gain > 0 ? "+" : ""}{fmt(a.r2_gain, 3)}
                </td>
                <td className="tabular py-2 text-right text-ink-muted">{fmt(a.holdout_rmse, 1)}</td>
              </tr>
            ))}
          </Table>

          {base && full && (
            <Verdict tone="warn">
              <strong>The projection alone gets holdout R² {fmt(base.holdout_r2, 3)}. Everything
              else added together gets {fmt(full.holdout_r2, 3)}.</strong> Last season's scoring,
              usage volume and the rookie flag contribute nothing measurable — the difference is{" "}
              {fmt(full.holdout_r2 - base.holdout_r2, 3)}, which is noise. That is a negative result,
              and it's reported because it's the finding: this projection is at its ceiling, and a
              better forecast isn't available from the features on hand.
            </Verdict>
          )}
        </>
      )}

      {vif.length > 0 && (
        <>
          <Sub>Why the feature set is what it is</Sub>
          <p className="mb-3 max-w-3xl text-sm leading-relaxed text-ink-muted">
            Variance inflation factors — regress each candidate on the others and read{" "}
            <code className="text-ink-muted">1 / (1 − R²)</code>. Above ~5 means a feature is largely
            redundant with the rest, which makes coefficients unstable and can flip their signs.
          </p>
          <div className="space-y-2">
            {vif.map((v) => (
              <div key={v.feature} className="grid grid-cols-[minmax(0,1fr)_5rem] items-center gap-3">
                <div className="flex items-center gap-2">
                  <code className="truncate text-xs text-ink-muted">{v.feature}</code>
                  {v.vif > 5 && (
                    <span className="shrink-0 rounded bg-amber-500/15 px-1.5 py-0.5 text-[10px] text-amber-300">
                      collinear
                    </span>
                  )}
                </div>
                <span className="tabular text-right text-xs text-ink-muted">{fmt(v.vif, 1)}</span>
              </div>
            ))}
          </div>
          <Note>
            <code className="text-ink-muted">projected_points</code> was dropped as a standalone
            regressor: <code className="text-ink-muted">proj_vorp</code> <em>is</em> projected points
            minus a per-position constant, so it already carries both the projection level and
            position scarcity. Keeping both is what produced the original notebook's sign-flipped,
            uninterpretable coefficient. The two usage terms are the most collinear things here (one
            is the other divided by games), so their individual coefficients should not be read as
            effect sizes — but they're kept, because the question is "does this add predictive
            value", and the ablation answers that directly on held-out data, where collinearity
            doesn't distort the verdict.
          </Note>
        </>
      )}
    </Section>
  );
}

// --- 11. bench depth -----------------------------------------------------

function BenchSection({ data }) {
  const rows = data.bench_depth?.availability || [];
  if (!rows.length) return null;
  const maxDepth = Math.max(...rows.map((r) => r.depth_value || 0), 1);
  return (
    <Section
      id="bench"
      icon={<Layers size={17} />}
      eyebrow="Live tool — depth"
      title="What a bench spot is actually worth"
      blurb={
        <>
          A 16-round draft with 9 starting slots is 7 bench picks — over a third of the draft. A
          backup only ever pays off in the weeks your starter isn't playing, so how often a
          position's starters miss games is the honest driver of how much depth there is worth
          having.
        </>
      }
    >
      <p className="mb-4 max-w-3xl text-sm leading-relaxed text-ink-muted">
        Measured on the top startable players by season points — the same replacement-level tier the
        VORP baseline uses. Restricting to that tier matters: including everyone on record would drag
        the rate toward deep-bench players who never appear at all, which measures irrelevance rather
        than injury.
      </p>
      <Table
        head={["Pos", "Games missed", "Mean games", "Depth value", ""]}
        align={["left", "right", "right", "right", "left"]}
      >
        {rows.map((r) => (
          <tr key={r.position} className="border-t border-line/60">
            <td className="py-2 pr-3"><PositionChip position={r.position} /></td>
            <td className="tabular py-2 pr-3 text-right text-ink-muted">{pctOf(r.missed_game_rate, 1)}</td>
            <td className="tabular py-2 pr-3 text-right text-ink-faint">{fmt(r.mean_games, 1)}</td>
            <td className="tabular py-2 pr-3 text-right font-semibold text-ink">
              {fmt(r.depth_value, 2)}
            </td>
            <td className="w-24 py-2">
              <ShareBar value={(r.depth_value || 0) / maxDepth} tone="bg-teal-400" />
            </td>
          </tr>
        ))}
      </Table>
      <Verdict tone="info">
        Depth value combines two things: how often the position's starters miss time, and how many of
        them you start (two RBs plus a share of FLEX is ~2.4× the exposure of a single QB). The
        result is that a backup running back is worth roughly twenty times a backup defense — which
        is why the board will suggest a fourth RB and will never suggest a second kicker.
      </Verdict>
      <Note>
        Three guards keep this honest: the bench term is capped below the starting-slot weight so a
        backup can never outrank filling a hole in your lineup; it decays with each extra body at a
        position, so the board won't stack six running backs; and it goes to zero when there's no
        bench room left. Once starters <em>and</em> bench are full, roster need stops separating
        players entirely and the board ranks on projected value alone.
      </Note>
    </Section>
  );
}

// --- 12. the live score --------------------------------------------------

const LiveScoreSection = ({ data }) => (
  <Section
    id="live"
    icon={<Target size={17} />}
    eyebrow="Putting it together"
    title="What the live board actually computes"
    blurb={
      <>
        Given that a better forecast isn't available, the board's job isn't to out-predict anyone.
        It's to apply the three things a projection contains no information about whatsoever — and
        to show its working, so a recommendation can be argued with rather than just obeyed.
      </>
    }
  >
    <div className="rounded-lg border border-line/60 bg-surface-raised px-4 py-3 text-center">
      <code className="text-sm text-ink">
        score = VORP × <span className="text-emerald-300">need</span> ×{" "}
        <span className="text-sky-300">timing</span> ×{" "}
        <span className="text-amber-300">confidence</span>
      </code>
    </div>

    <div className="mt-4 grid gap-4 sm:grid-cols-2">
      <Term title="Value (VORP)" tone="text-ink">
        Projected points above replacement, then dampened across positions so a quarterback's steep
        cliff to replacement level doesn't make every QB look like a first-rounder. Raw VORP isn't
        comparable between positions scoring on different scales.
      </Term>
      <Term title="Roster need" tone="text-emerald-300">
        Open starting slots, escalating as your picks run out — a slot with twelve picks left is
        barely a constraint; with two it's the whole decision. Plus what a bench spot at that
        position is worth once the lineup is full. FLEX is split across eligible positions rather
        than ignored, and surplus players count as depth instead of inflating "have".
      </Term>
      <Term title="Pick timing" tone="text-sky-300">
        The question is always "grab them now, or will they last?" — so the board estimates the
        probability a player survives to your <em>next</em> turn, treating their draft slot as a
        normal distribution that widens deeper into the draft. Pick 8 is far more predictable than
        pick 140.
      </Term>
      <Term title="Confidence" tone="text-amber-300">
        Blends how much a model fit wobbles across resamples with how wrong projections at that
        position actually turn out to be, plus the unproven-player factor. Bounded so it breaks ties
        between close players rather than overriding a real value gap — the "Play it safe" slider
        sets that bound, and zero disables it.
      </Term>
    </div>

    <Verdict>
      Each multiplier is returned to the interface separately rather than collapsed into one number,
      which is why every row can say <em>which</em> factor is driving it — "Fills a big need", "Now
      or never", "Useful depth", "Unproven". A recommendation you can't interrogate is one you can't
      reasonably disagree with, and disagreeing with it is the point: the board is an argument, not
      an oracle.
    </Verdict>
  </Section>
);

const Term = ({ title, tone, children }) => (
  <div>
    <h4 className={`mb-1.5 text-sm font-semibold ${tone}`}>{title}</h4>
    <p className="text-xs leading-relaxed text-ink-muted">{children}</p>
  </div>
);
