import React, { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  BarChart3,
  CalendarClock,
  ChevronDown,
  Info,
  LayoutList,
  Radio,
  Maximize2,
  RefreshCw,
  Search,
  ShieldCheck,
  Sparkles,
  Timer,
  Trophy,
  UserPlus,
  Users,
  WifiOff,
  XCircle,
} from "lucide-react";

// The Analysis tab is the largest source file in the app and the board always
// opens first, so it's split out of the initial chunk rather than shipped to
// every user who never clicks it.
const Analysis = lazy(() => import("./components/Analysis"));
import { LEGEND, RangeText, ReasonChips } from "./components/explain";
import PlayerDetail from "./components/PlayerDetail";
import { Badge, Button, Meter, PositionChip, SlotPips } from "./components/primitives";
import { confidenceBand, fmt, fmtAdp, NO_ADP, pct, urgencyBand } from "./theme";

const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";
const POSITIONS = ["ALL", "QB", "RB", "WR", "TE", "K", "DST"];
const DEFAULT_ROSTER_NEED = { QB: 1, RB: 2, WR: 2, TE: 1, FLEX: 1, K: 1, DST: 1 };
const RECOMMEND_TOPN = 300; // effectively "all remaining candidates", so search/filter has the full pool
const DEFAULT_ROUNDS = 16;
const DEFAULT_RISK_AVERSION = 0.2;

// ---- Snake-draft math ----
// Round 1 goes slot 1..teams, round 2 goes teams..1, etc.
function slotForPick(pickNumber, teams) {
  const round = Math.floor((pickNumber - 1) / teams) + 1;
  const posInRound = ((pickNumber - 1) % teams) + 1;
  return round % 2 === 1 ? posInRound : teams - posInRound + 1;
}

function nextMyPick(fromPick, mySlot, teams) {
  let n = fromPick;
  while (slotForPick(n, teams) !== mySlot) n += 1;
  return n;
}

// ---- Device memory ----
//
// Only the opaque token is kept here; the ESPN cookies stay on the server. See
// src/auth.py for why. `lastLeagueId` is what makes reopening the app land on
// the board instead of the league picker.
const STORE_KEY = "jda.device";

function loadDevice() {
  try {
    return JSON.parse(localStorage.getItem(STORE_KEY) || "{}") || {};
  } catch {
    return {};   // private mode, or someone hand-edited it
  }
}

function saveDevice(patch) {
  try {
    localStorage.setItem(STORE_KEY, JSON.stringify({ ...loadDevice(), ...patch }));
  } catch { /* remembering is a convenience; never break the app over it */ }
}

// Read once at load and kept in a module variable so every request carries it
// without threading the token through a dozen call sites.
let deviceToken = loadDevice().token || null;

const authHeaders = () => (deviceToken ? { "X-Device-Token": deviceToken } : {});

const apiGet = (path) => fetch(`${API_URL}${path}`, { headers: authHeaders() });

async function apiPost(path, body) {
  const res = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!res.ok) {
    // The detail is the whole value of the auth errors - "check your cookies"
    // versus "ESPN is down" send someone to different places.
    let detail = null;
    try { detail = (await res.json()).detail; } catch { /* not JSON */ }
    const e = new Error(detail || `HTTP ${res.status}`);
    e.status = res.status;
    throw e;
  }
  return res.json();
}

// ---- Draft scheduling ----
//
// `draft_at` is null when ESPN has no date on file. That absence is the entire
// "not scheduled" signal - ESPN sends no sentinel date and no flag - so it is
// the only thing tested for here.

const fmtDraftDate = (ms) =>
  new Date(ms).toLocaleString(undefined, {
    weekday: "short", day: "numeric", month: "short",
    hour: "numeric", minute: "2-digit",
  });

/** "in 3d 4h" / "in 12m" / "underway". Null once there's no date. */
function countdownTo(ms, now = Date.now()) {
  if (!ms) return null;
  const left = ms - now;
  if (left <= 0) return "underway";
  const m = Math.floor(left / 60000);
  const d = Math.floor(m / 1440);
  const h = Math.floor((m % 1440) / 60);
  if (d > 0) return `in ${d}d ${h}h`;
  if (h > 0) return `in ${h}h ${m % 60}m`;
  return `in ${m}m`;
}

const DraftWhenChip = ({ draftAt, scheduled, className = "" }) =>
  scheduled && draftAt ? (
    <span
      className={`shrink-0 rounded-md bg-sky-500/15 px-2 py-1 text-[11px] text-sky-300 ${className}`}
      title={`Draft scheduled for ${fmtDraftDate(draftAt)}`}
    >
      {fmtDraftDate(draftAt)}
    </span>
  ) : (
    <span
      className={`shrink-0 rounded-md bg-white/5 px-2 py-1 text-[11px] text-slate-500 ${className}`}
      title="ESPN has no draft date for this league yet."
    >
      Not scheduled
    </span>
  );

// ---- League switcher ----
//
// Switching used to mean going home first. The name in the header is the menu,
// so the league you're looking at and the control that changes it are the same
// thing.
function LeagueMenu({ leagues, currentId, currentName, onSwitch, onSignOut }) {
  const [open, setOpen] = useState(false);
  const box = useRef(null);

  useEffect(() => {
    if (!open) return;
    const away = (e) => { if (!box.current?.contains(e.target)) setOpen(false); };
    const esc = (e) => e.key === "Escape" && setOpen(false);
    document.addEventListener("mousedown", away);
    document.addEventListener("keydown", esc);
    return () => {
      document.removeEventListener("mousedown", away);
      document.removeEventListener("keydown", esc);
    };
  }, [open]);

  const list = leagues || [];

  return (
    <div className="relative" ref={box}>
      <button
        onClick={() => setOpen((v) => !v)}
        title="Switch league"
        className="flex max-w-[14rem] items-center gap-1.5 rounded-lg border border-white/10 bg-slate-900
          px-2.5 py-1.5 text-xs text-slate-300 transition hover:border-white/20 hover:text-white"
      >
        <Users size={12} className="shrink-0 text-slate-500" />
        <span className="truncate">{currentName || "League"}</span>
        <ChevronDown size={12} className={`shrink-0 ${open ? "rotate-180 transition" : "transition"}`} />
      </button>

      {open && (
        <div className="absolute left-0 top-full z-30 mt-1.5 w-72 overflow-hidden rounded-xl border
          border-white/10 bg-slate-900 shadow-xl shadow-black/40">
          <div className="px-3 py-2 text-[10px] uppercase tracking-wider text-slate-600">
            Your leagues
          </div>
          {list.length === 0 && (
            <div className="px-3 pb-3 text-xs text-slate-500">No leagues found.</div>
          )}
          {list.map((l) => {
            const active = String(l.league_id) === String(currentId);
            return (
              <button
                key={l.league_id}
                onClick={() => { setOpen(false); if (!active) onSwitch(l.league_id); }}
                className={`flex w-full items-center gap-2 px-3 py-2 text-left transition ${
                  active ? "bg-emerald-500/10" : "hover:bg-white/5"
                }`}
              >
                <span className={`flex-1 truncate text-xs ${active ? "text-emerald-300" : "text-slate-300"}`}>
                  {l.name}
                </span>
                <DraftWhenChip draftAt={l.draft_at} scheduled={l.scheduled} />
              </button>
            );
          })}
          <button
            onClick={() => { setOpen(false); onSignOut(); }}
            className="w-full border-t border-white/5 px-3 py-2 text-left text-xs text-slate-500
              transition hover:bg-white/5 hover:text-slate-300"
          >
            Sign out of ESPN
          </button>
        </div>
      )}
    </div>
  );
}

// ---- Sign in ----
//
// The credentials go to the backend, which keeps them and returns an opaque
// token. Only that token is stored on the device, so a script on this page
// can't read the ESPN session cookies and they don't travel on every request.

function SignInScreen({ onSignIn, busy, error }) {
  const [swid, setSwid] = useState("");
  const [s2, setS2] = useState("");
  const [how, setHow] = useState(false);

  const field =
    "mt-1.5 h-10 w-full rounded-lg border border-white/10 bg-slate-900 px-3 font-mono text-xs " +
    "text-slate-100 outline-none transition focus:border-emerald-400/50 focus:ring-2 focus:ring-emerald-400/20";

  return (
    <div className="flex min-h-screen items-center justify-center p-6">
      <div className="card w-full max-w-md p-7">
        <div className="mb-1 flex items-center gap-2">
          <Trophy className="text-emerald-400" size={20} />
          <h1 className="text-xl font-semibold tracking-tight">Justin's Draft Assistant</h1>
        </div>
        <p className="mb-6 text-sm leading-relaxed text-slate-400">
          Connect your ESPN account to load your leagues, follow your draft live, and analyse how
          your league drafts.
        </p>

        <form
          onSubmit={(e) => { e.preventDefault(); onSignIn(swid.trim(), s2.trim()); }}
          className="space-y-3"
        >
          <label className="block text-sm text-slate-300">
            SWID
            <input
              value={swid} onChange={(e) => setSwid(e.target.value)}
              placeholder="{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}"
              className={field} spellCheck={false} autoComplete="off"
            />
          </label>
          <label className="block text-sm text-slate-300">
            espn_s2
            <input
              value={s2} onChange={(e) => setS2(e.target.value)}
              placeholder="AEB..."
              className={field} spellCheck={false} autoComplete="off"
            />
          </label>

          {error && (
            <div className="rounded-lg border border-rose-400/30 bg-rose-500/10 px-3 py-2 text-xs leading-relaxed text-rose-300">
              {error}
            </div>
          )}

          <button
            type="submit"
            disabled={busy || !swid.trim() || !s2.trim()}
            className="h-11 w-full rounded-lg bg-emerald-500 text-sm font-semibold text-slate-950
              transition hover:bg-emerald-400 disabled:opacity-50"
          >
            {busy ? "Connecting…" : "Connect ESPN account"}
          </button>
        </form>

        <button
          onClick={() => setHow((v) => !v)}
          className="mt-4 flex w-full items-center justify-center gap-1 text-xs text-slate-500 hover:text-slate-300"
        >
          Where do I find these? <ChevronDown size={12} className={how ? "rotate-180 transition" : "transition"} />
        </button>
        {how && (
          <ol className="mt-3 space-y-1.5 rounded-lg bg-white/[0.03] p-3 text-xs leading-relaxed text-slate-400">
            <li>1. Sign in at <span className="text-slate-300">fantasy.espn.com</span> in this browser.</li>
            <li>2. Open developer tools → Application → Cookies → espn.com.</li>
            <li>3. Copy the values of <span className="text-slate-300">SWID</span> and <span className="text-slate-300">espn_s2</span>.</li>
          </ol>
        )}

        <p className="mt-5 text-[11px] leading-relaxed text-slate-600">
          These are stored on the server running this app and remembered on this device, so you
          only do it once. They are never kept in your browser and never sent back to it.
        </p>
      </div>
    </div>
  );
}

// ---- Session setup ----
function SetupScreen({ onStart, starting, error, leagues, leaguesError, onSignOut }) {
  const [teams, setTeams] = useState(14);
  const [mySlot, setMySlot] = useState(1);
  const [rounds, setRounds] = useState(DEFAULT_ROUNDS);
  // ESPN is the primary path; the manual form is revealed on request.
  //
  // The teams/slot inputs are deliberately absent from the ESPN path rather
  // than pre-filled: a hand-entered slot that disagrees with the league
  // silently poisons `next_pick`, and that drives the whole availability term.
  // Better to ask the league than to ask the user and hope.
  const [manual, setManual] = useState(false);

  const field =
    "mt-1.5 h-10 w-full rounded-lg border border-white/10 bg-slate-900 px-3 text-sm text-slate-100 " +
    "outline-none transition focus:border-emerald-400/50 focus:ring-2 focus:ring-emerald-400/20";

  return (
    <div className="flex min-h-screen items-center justify-center p-6">
      <div className="card w-full max-w-md p-7">
        <div className="mb-1 flex items-center gap-2">
          <Trophy className="text-emerald-400" size={20} />
          <h1 className="text-xl font-semibold tracking-tight">Justin's Draft Assistant</h1>
        </div>
        <p className="mb-6 text-sm leading-relaxed text-slate-400">
          Live recommendations that account for your open roster slots, how long a player will
          last, and how much the projection can be trusted.
        </p>

        {!manual && (
          <>
            {leaguesError ? (
              <div className="rounded-lg border border-rose-400/30 bg-rose-500/10 px-3 py-2.5 text-xs leading-relaxed text-rose-300">
                Couldn't reach ESPN. Check SWID and ESPN_S2 in <code>.env</code>, or set up manually.
              </div>
            ) : leagues === null ? (
              <div className="py-3 text-sm text-slate-500">Finding your leagues…</div>
            ) : leagues.length === 0 ? (
              <div className="text-sm text-slate-500">No football leagues found for this season.</div>
            ) : (
              <>
                <div className="label mb-2">Your leagues</div>
                <div className="space-y-2">
                  {leagues.map((lg) => (
                    <button
                      key={lg.league_id}
                      onClick={() => onStart({ espn: true, leagueId: lg.league_id })}
                      disabled={starting}
                      className="flex w-full items-center justify-between gap-3 rounded-lg border
                        border-white/10 bg-slate-900 px-3.5 py-3 text-left transition
                        hover:border-emerald-400/40 hover:bg-slate-800/60 disabled:opacity-50"
                    >
                      <span className="min-w-0">
                        <span className="block truncate text-sm font-semibold text-slate-100">
                          {lg.name}
                        </span>
                        <span className="mt-0.5 block text-[11px] text-slate-500">
                          {lg.has_history
                            ? "Draft history collected — league timing applied"
                            : "No draft history — market ADP timing only"}
                        </span>
                      </span>
                      <DraftWhenChip draftAt={lg.draft_at} scheduled={lg.scheduled} />
                    </button>
                  ))}
                </div>
              </>
            )}
            {error && (
              <div className="mt-4 rounded-lg border border-rose-400/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
                {error}
              </div>
            )}
            <button
              onClick={() => setManual(true)}
              className="mt-5 w-full text-xs text-slate-500 underline-offset-2 hover:text-slate-300 hover:underline"
            >
              Set up manually instead
            </button>
          </>
        )}

        {manual && (
        <div className="space-y-4">
          <label className="block text-sm text-slate-300">
            Number of teams
            <input
              type="number" min={2} max={20} value={teams}
              onChange={(e) => setTeams(Number(e.target.value))}
              className={field}
            />
          </label>
          <label className="block text-sm text-slate-300">
            Your draft slot
            <input
              type="number" min={1} max={teams} value={mySlot}
              onChange={(e) => setMySlot(Number(e.target.value))}
              className={field}
            />
            <span className="mt-1 block text-xs text-slate-500">1 = first overall pick</span>
          </label>
          <label className="block text-sm text-slate-300">
            Rounds
            <input
              type="number" min={1} max={30} value={rounds}
              onChange={(e) => setRounds(Number(e.target.value))}
              className={field}
            />
            <span className="mt-1 block text-xs text-slate-500">
              Lets the board push harder on unfilled starting slots as your picks run out
            </span>
          </label>

        {error && (
          <div className="mt-4 rounded-lg border border-rose-400/30 bg-rose-500/10 px-3 py-2 text-xs text-rose-300">
            {error}
          </div>
        )}

        <button
          onClick={() => onStart({ teams, mySlot, rounds })}
          disabled={starting}
          className="mt-6 h-11 w-full rounded-lg bg-emerald-500 text-sm font-semibold text-slate-950
            transition hover:bg-emerald-400 disabled:opacity-50"
        >
          {starting ? "Starting…" : "Start draft"}
        </button>
        <button
          onClick={() => setManual(false)}
          className="mt-4 w-full text-xs text-slate-500 underline-offset-2 hover:text-slate-300 hover:underline"
        >
          Connect to ESPN instead
        </button>
        </div>
        )}

        <p className="mt-4 text-xs leading-relaxed text-slate-500">
          If the backend at <code className="text-slate-400">{API_URL}</code> isn't reachable, this
          falls back to a read-only board from the last exported rankings.
        </p>
      </div>
    </div>
  );
}

// ---- Expanded row ----
// The detail shown inline when a row is clicked open. Named for what it is
// rather than "PlayerCard", which was confusable with PlayerDetail (the full
// side panel). It used to double as a hero card above the board; that is gone,
// because a recommendation displayed outside the list it heads can't show you
// where in the ranking it actually sits.
function ExpandedPlayer({ player, nextPick, onDraft, onTaken, onOpenFull, readOnly }) {
  if (!player) return null;
  const urgency = urgencyBand(player.availability);
  const conf = confidenceBand(player.confidence);
  const isRookie = Number(player.is_rookie) === 1;

  return (
    <div className="relative px-4 py-5">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            {/* Not truncated: the player's name is the single most important
                thing on this card, and "Malik Na…" is useless at a glance. */}
            <span className="text-2xl font-semibold leading-tight tracking-tight">
              {player.player_name}
            </span>
            <PositionChip position={player.position} />
            <span className="text-sm text-slate-500">{player.pro_team}</span>
            {isRookie && (
              <Badge tone="warn" title="No prior-season production behind the projection.">
                <Sparkles size={10} /> Unproven
              </Badge>
            )}
          </div>
          <div className="mt-3">
            {/* "Unproven" is already a badge next to the name above. */}
            <ReasonChips player={player} nextPick={nextPick} exclude={["Unproven"]} />
          </div>
        </div>

        <div className="flex flex-wrap gap-2">
          <Button onClick={onOpenFull} size="md" title="Full breakdown: last season, range, scoring">
            <Maximize2 size={14} /> Full details
          </Button>
          {!readOnly && (
            <>
              <Button onClick={onDraft} tone="solid" size="md" title="Draft to my team">
                <UserPlus size={14} /> Draft
              </Button>
              <Button onClick={onTaken} tone="danger" size="md" title="Someone else took them">
                <XCircle size={14} /> Taken
              </Button>
            </>
          )}
        </div>
      </div>

      <div className="mt-5 grid grid-cols-2 gap-x-6 gap-y-4 border-t border-white/5 pt-4 sm:grid-cols-4">
        <Stat label="Score" value={fmt(player.utility, 0)} accent />
        <Stat label="VORP" value={fmt(player.vorp_z ?? player.vorp, 0)} />
        <Stat label="ADP" value={fmtAdp(player.adp)} />
        <Stat label="Range" value={<RangeText player={player} />} />
      </div>

      {(urgency || conf) && (
        <div className="mt-4 grid gap-4 sm:grid-cols-2">
          {urgency && (
            <MeterRow
              icon={<Timer size={12} />}
              label="Still there at your next pick"
              value={pct(player.availability)}
              meter={player.availability}
              tone={urgency.tone}
              caption={urgency.label}
            />
          )}
          {conf && (
            <MeterRow
              icon={<ShieldCheck size={12} />}
              label="Confidence in the projection"
              value={pct(player.confidence)}
              meter={player.confidence}
              tone={conf.tone}
              caption={conf.label}
            />
          )}
        </div>
      )}
    </div>
  );
}

const Stat = ({ label, value, accent }) => (
  <div>
    <div className="label">{label}</div>
    <div className={`tabular mt-0.5 text-lg font-semibold ${accent ? "text-emerald-400" : "text-slate-200"}`}>
      {value}
    </div>
  </div>
);

const MeterRow = ({ icon, label, value, meter, tone, caption }) => (
  <div>
    <div className="mb-1.5 flex items-center justify-between gap-2">
      <span className="label flex items-center gap-1.5">
        {icon} {label}
      </span>
      <span className="tabular text-xs font-semibold text-slate-300">{value}</span>
    </div>
    <Meter value={meter} tone={tone} />
    <div className="mt-1 text-[11px] text-slate-500">{caption}</div>
  </div>
);

// ---- Board row ----
// Memoised: up to ~300 of these render at once, and typing, sorting, filtering
// and expanding a row all used to re-render every one of them. The handlers are
// shared across rows and take the player as an argument, so this component
// supplies its own identity when calling them.
const PlayerRow = React.memo(function PlayerRow({
  player, rank, nextPick, onDraft, onTaken, onToggle, onOpenFull, expanded, readOnly, highlight,
}) {
  const urgency = urgencyBand(player.availability);
  const conf = confidenceBand(player.confidence);
  const isRookie = Number(player.is_rookie) === 1;

  // The whole row expands inline, but the Draft/Taken buttons live inside it -
  // stopPropagation on the actions cell keeps a draft click from also toggling
  // the row open.
  return (
    <>
    <tr
      id={`row-${player.player_id}`}
      onClick={() => onToggle(player.player_id)}
      className={`group cursor-pointer border-t border-white/5 transition-colors hover:bg-white/[0.03]
        ${expanded ? "bg-white/[0.04]" : ""}
        ${highlight ? "border-l-2 border-l-emerald-400/70 bg-emerald-500/[0.04]" : ""}`}
    >
      <td className="py-2.5 pl-4 pr-2 text-right">
        <span className="tabular text-xs text-slate-600">{rank}</span>
      </td>
      <td className="py-2.5 pr-3">
        <div className="flex items-center gap-2">
          {highlight && (
            <Trophy size={11} className="shrink-0 text-emerald-400" aria-label="Board's top pick" />
          )}
          <span className="truncate font-medium text-slate-100">{player.player_name}</span>
          <span className="shrink-0 text-xs text-slate-600">{player.pro_team}</span>
          {isRookie && (
            <Sparkles
              size={11}
              className="shrink-0 text-amber-400"
              aria-label="Rookie"
            />
          )}
        </div>
        <div className="mt-1 hidden sm:block">
          <ReasonChips player={player} nextPick={nextPick} max={2} />
        </div>
      </td>
      <td className="py-2.5 pr-3">
        <PositionChip position={player.position} />
      </td>
      {/* VORP and ADP are the inputs; Available, Confidence and Score are the
          decision. On narrow screens the inputs give way so the decision
          columns stay on screen without horizontal scrolling. */}
      <td className="tabular hidden py-2.5 pr-3 text-right text-slate-400 md:table-cell">
        {fmt(player.vorp_z ?? player.vorp, 0)}
      </td>
      <td className="tabular hidden py-2.5 pr-3 text-right text-slate-400 md:table-cell">
        {fmtAdp(player.adp)}
      </td>
      <td className="hidden py-2.5 pr-3 sm:table-cell">
        <div className="flex min-w-[64px] flex-col gap-1">
          <span className="tabular text-right text-xs text-slate-400">{pct(player.availability)}</span>
          <Meter value={player.availability} tone={urgency?.tone || "neutral"} />
        </div>
      </td>
      <td className="hidden py-2.5 pr-3 sm:table-cell">
        <div className="flex min-w-[64px] flex-col gap-1">
          <span className="tabular text-right text-xs text-slate-400">{pct(player.confidence)}</span>
          <Meter value={player.confidence} tone={conf?.tone || "neutral"} />
        </div>
      </td>
      <td className="tabular py-2.5 pr-3 text-right font-semibold text-emerald-400">
        {fmt(player.utility, 0)}
      </td>
      <td className="py-2.5 pl-2 pr-4" onClick={(e) => e.stopPropagation()}>
        <div className="flex items-center justify-end gap-1">
          {!readOnly && (
            <div className="flex gap-1 opacity-60 transition-opacity group-hover:opacity-100">
              <Button onClick={() => onDraft(player)} tone="primary" title="Draft to my team">
                <UserPlus size={12} /> Me
              </Button>
              <Button onClick={() => onTaken(player)} tone="danger" title="Someone else took them">
                <XCircle size={12} /> Taken
              </Button>
            </div>
          )}
          <ChevronDown
            size={14}
            onClick={() => onToggle(player.player_id)}
            className={`shrink-0 cursor-pointer text-slate-600 transition-transform
              ${expanded ? "rotate-180 text-slate-300" : ""}`}
          />
        </div>
      </td>
    </tr>
    {expanded && (
      <tr>
        <td colSpan={9} className="border-t border-white/5 bg-slate-950/40 p-0">
          <ExpandedPlayer
            player={player}
            nextPick={nextPick}
            readOnly={readOnly}
            onOpenFull={() => onOpenFull(player.player_id)}
            onDraft={() => onDraft(player)}
            onTaken={() => onTaken(player)}
          />
        </td>
      </tr>
    )}
    </>
  );
});
// ---- Draft day ----
//
// Everything that's knowable before a pick is made. Deliberately a tab rather
// than a mode: the board stays fully reachable for a league whose draft has no
// date yet, which is most of them most of the year.
//
// Note what does NOT live here: the switch to "the draft is live" is driven by
// the first real pick appearing, never by this countdown reaching zero. Drafts
// start late, get paused, and the scheduled time is a plan rather than a fact.

/** The countdown, broken into units so it can be read at a glance. */
function CountdownClock({ draftAt, now }) {
  const left = Math.max(draftAt - now, 0);
  const total = Math.floor(left / 1000);
  const units = [
    { label: "days", value: Math.floor(total / 86400) },
    { label: "hours", value: Math.floor((total % 86400) / 3600) },
    { label: "mins", value: Math.floor((total % 3600) / 60) },
    { label: "secs", value: total % 60 },
  ];
  // Leading zero units are noise twelve days out; drop them until one matters.
  const firstSignificant = units.findIndex((u) => u.value > 0);
  let shown = units.slice(firstSignificant === -1 ? 2 : Math.min(firstSignificant, 2));
  // Seconds only inside the final hour, which is also the only time the clock
  // ticks that fast. Showing a seconds digit that moves twice a minute reads as
  // a broken page rather than a slow one.
  if (total >= 3600) shown = shown.filter((u) => u.label !== "secs");

  return (
    <div className="flex flex-wrap items-end gap-x-5 gap-y-2">
      {shown.map((u) => (
        <div key={u.label} className="flex items-baseline gap-1.5">
          <span className="tabular text-5xl font-semibold leading-none tracking-tight text-slate-50">
            {String(u.value).padStart(2, "0")}
          </span>
          <span className="text-xs uppercase tracking-wider text-slate-500">{u.label}</span>
        </div>
      ))}
    </div>
  );
}

function DraftDay({ league, pickCtx, order, teamsList, rosterState, picksRemaining, myTeamId }) {
  const [now, setNow] = useState(Date.now());
  const draftAt = league?.draft_at;
  // Every second inside the last hour, every thirty otherwise. A seconds
  // display that only moves twice a minute reads as broken, and a page that
  // re-renders every second for twelve days is pure waste.
  useEffect(() => {
    if (!draftAt) return;
    const near = draftAt - Date.now() < 3600_000;
    const id = setInterval(() => setNow(Date.now()), near ? 1000 : 30000);
    return () => clearInterval(id);
  }, [draftAt, now < draftAt - 3600_000]);

  const names = useMemo(() => {
    const m = new Map();
    (teamsList || []).forEach((t) => m.set(t.id, t));
    return m;
  }, [teamsList]);

  const teams = pickCtx?.teams || 14;
  const roundOne = (order || []).slice(0, teams);
  const open = Object.entries(rosterState || {}).filter(([, v]) => v.need - v.have > 0);
  const underway = draftAt && draftAt - now <= 0;

  return (
    <div className="mx-auto max-w-5xl space-y-5 p-4">
      {/* The countdown is the page, not a line inside a card - it's the one
          thing you open this tab for. */}
      <div className="card px-6 py-7">
        <div className="label mb-3 text-slate-500">
          {league?.name || "Your draft"} · draft day
        </div>

        {league?.scheduled ? (
          underway ? (
            <div className="flex flex-wrap items-baseline gap-3">
              <span className="text-4xl font-semibold tracking-tight text-emerald-400">
                Draft should be underway
              </span>
              <span className="text-sm text-slate-500">{fmtDraftDate(draftAt)}</span>
            </div>
          ) : (
            <>
              <CountdownClock draftAt={draftAt} now={now} />
              <p className="mt-4 text-sm text-slate-400">
                {fmtDraftDate(draftAt)} · the board switches over on its own when the first pick
                lands, not when this hits zero.
              </p>
            </>
          )
        ) : (
          <>
            <div className="text-4xl font-semibold tracking-tight text-slate-300">
              Not scheduled yet
            </div>
            <p className="mt-4 max-w-2xl text-sm leading-relaxed text-slate-400">
              Your commissioner hasn't set a date, so there's nothing to count down to. The order
              below is already fixed, though, and the board is live and ready whenever it starts.
            </p>
          </>
        )}
      </div>

      <div className="grid gap-5 lg:grid-cols-2">
        <div className="card p-5">
          <h2 className="mb-1 text-sm font-semibold text-slate-200">Your picks</h2>
          <p className="mb-3 text-xs leading-relaxed text-slate-500">
            Every slot you own, straight from the league's own order — so this survives traded
            picks and keepers rather than assuming a clean snake.
          </p>
          <div className="flex flex-wrap gap-1.5">
            {(pickCtx?.myPicks || []).map((n, i) => (
              <span
                key={n}
                className={`tabular rounded-md px-2 py-1 text-xs ${
                  i === 0 ? "bg-emerald-500/15 text-emerald-300 ring-1 ring-emerald-400/25"
                          : "bg-white/[0.04] text-slate-300"}`}
                title={i === 0 ? "Your first pick" : `Round ${i + 1}`}
              >
                {n}
              </span>
            ))}
          </div>
          {!pickCtx?.myPicks?.length && (
            <div className="text-xs text-slate-500">Not available.</div>
          )}
        </div>

        <div className="card p-5">
          <h2 className="mb-1 text-sm font-semibold text-slate-200">Still to fill</h2>
          <p className="mb-3 text-xs leading-relaxed text-slate-500">
            Starting slots open, against the picks you have left.
          </p>
          <div className="flex flex-wrap items-center gap-1.5">
            {open.length === 0 ? (
              <span className="text-xs text-slate-500">Starting lineup complete.</span>
            ) : (
              open.map(([pos, v]) => (
                <span key={pos} className="flex items-center gap-1 rounded-md bg-white/[0.04] px-2 py-1">
                  <PositionChip position={pos} />
                  <span className="tabular text-xs text-slate-400">×{v.need - v.have}</span>
                </span>
              ))
            )}
          </div>
          {picksRemaining != null && (
            <p className="mt-3 text-xs text-slate-500">
              <span className="tabular font-semibold text-slate-300">{picksRemaining}</span> picks left
            </p>
          )}
        </div>
      </div>

      <div className="card p-5">
        <h2 className="mb-1 text-sm font-semibold text-slate-200">Round 1 order</h2>
        <p className="mb-3 text-xs leading-relaxed text-slate-500">
          Known before the draft starts — ESPN publishes the full slot order as soon as the league
          is set up, which is also where your pick numbers come from.
        </p>
        <div className="space-y-1">
          {roundOne.map((teamId, i) => {
            const t = names.get(teamId);
            const mine = teamId === myTeamId;
            return (
              <div
                key={i}
                className={`flex items-center gap-3 rounded-md px-2.5 py-1.5 text-sm ${
                  mine ? "bg-emerald-500/10 ring-1 ring-emerald-400/25" : ""}`}
              >
                <span className="tabular w-6 text-right text-xs text-slate-600">{i + 1}</span>
                <span className={mine ? "font-semibold text-emerald-200" : "text-slate-300"}>
                  {t?.name || `Team ${teamId}`}
                </span>
                {mine && <span className="text-[11px] text-emerald-400/70">you</span>}
                {t?.abbrev && <span className="ml-auto text-[11px] text-slate-600">{t.abbrev}</span>}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

// ---- Board header ----
//
// One block rather than three stacked strips. The three-strip version wrapped
// onto a second line whenever a player's name ran long, which both looked
// broken and cost the vertical space the list needs - the whole reason the
// original hero card was removed. A fixed three-column grid cannot wrap.

function BoardHeader({
  mode, pickCtx, sync, onDisconnect, players, nextPick, readOnly, customSort,
  onOpen, onDraft, onTaken, draftLog,
}) {
  const offline = mode === "offline";
  const league = sync?.league;
  const round = pickCtx?.currentPick && pickCtx?.teams
    ? Math.ceil(pickCtx.currentPick / pickCtx.teams) : null;

  return (
    <div className="card overflow-hidden">
      {/* Row 1: which league, what state, whose turn. */}
      <div className={`flex flex-wrap items-center gap-x-3 gap-y-2 border-b border-white/5 px-4 py-2.5 text-xs
        ${pickCtx?.isMyTurn && !offline ? "bg-emerald-500/[0.06]" : ""}`}>
        {offline ? (
          <Badge tone="warn" title="The backend isn't running, so need and timing can't be applied.">
            <WifiOff size={11} /> Offline — value only
          </Badge>
        ) : (
          <>
            <span className="flex items-center gap-2">
              <span className="text-sm font-semibold text-slate-100">
                {league?.name || "Draft board"}
              </span>
              {league && (
                <DraftWhenChip draftAt={league.draft_at} scheduled={league.scheduled} />
              )}
            </span>

            <span className="h-3 w-px bg-white/10" />

            <span className="flex items-center gap-1.5">
              <span className="text-slate-500">Pick</span>
              <span className="tabular font-semibold text-slate-100">
                {pickCtx?.currentPick ?? "—"}
              </span>
              {round && <span className="text-slate-600">· Rd {round}</span>}
            </span>

            {pickCtx?.isMyTurn ? (
              <Badge tone="calm">You're on the clock</Badge>
            ) : (
              <span className="text-slate-400">
                you're up at{" "}
                <span className="tabular font-semibold text-slate-200">
                  {pickCtx?.thisTurn ?? "—"}
                </span>
                {pickCtx?.picksUntilMyTurn != null && (
                  <span className="text-slate-600"> · {pickCtx.picksUntilMyTurn} away</span>
                )}
              </span>
            )}
          </>
        )}

        <div className="ml-auto flex items-center gap-2">
          {/* Bias is measured on one league's drafters. Saying so is the point:
              a board with no league timing shouldn't look identical to one
              that has it. */}
          {league && league.has_history === false && (
            <span
              className="rounded-md bg-white/5 px-2 py-0.5 text-[11px] text-slate-500"
              title="No draft history has been collected for this league, so pick timing uses national ADP only. League-specific tendencies are only applied where they were measured."
            >
              Market timing only
            </span>
          )}
          <SyncChip sync={sync} onDisconnect={onDisconnect} />
        </div>
      </div>

      {/* Row 2: the board's own top three, as equal cards. */}
      {players?.length > 0 && (
        <div className="px-4 py-3">
          <div className="label mb-2 flex items-center gap-1.5 text-emerald-400/70">
            <Trophy size={11} />
            <span title={customSort ? "Unaffected by the table's current sort." : undefined}>
              Board ranking
            </span>
          </div>
          <div className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
            {players.map((p, i) => (
              <TopPickCard
                key={p.player_id}
                player={p}
                rank={i + 1}
                nextPick={nextPick}
                readOnly={readOnly}
                onOpen={onOpen}
                onDraft={onDraft}
                onTaken={onTaken}
              />
            ))}
          </div>
        </div>
      )}

      {mode === "live" && draftLog?.length > 0 && <RecentPicksTicker draftLog={draftLog} />}
    </div>
  );
}

/** One of the top three. Fixed height, so the row can never reflow. */
function TopPickCard({ player, rank, nextPick, readOnly, onOpen, onDraft, onTaken }) {
  const top = rank === 1;
  return (
    <div
      className={`flex min-w-0 flex-col justify-between rounded-lg px-3 py-2.5 transition
        ${top ? "bg-emerald-500/10 ring-1 ring-emerald-400/25" : "bg-white/[0.04] hover:bg-white/[0.06]"}`}
    >
      <div className="flex items-start justify-between gap-2">
        <button
          onClick={() => onOpen(player.player_id)}
          className="min-w-0 text-left"
          title="Show this player in the list"
        >
          <span className="flex items-baseline gap-1.5">
            <span className="tabular text-[11px] text-slate-600">{rank}</span>
            <span className="truncate text-sm font-semibold text-slate-100 hover:text-emerald-300">
              {player.player_name}
            </span>
          </span>
          <span className="mt-1 flex items-center gap-1.5">
            <PositionChip position={player.position} />
            <span className="text-[11px] text-slate-600">{player.pro_team}</span>
          </span>
        </button>
        <span className="tabular shrink-0 text-lg font-semibold leading-none text-emerald-400">
          {fmt(player.utility, 0)}
        </span>
      </div>

      <div className="mt-2 flex min-h-[22px] items-center justify-between gap-2">
        {/* One chip: even in miniature the board explains rather than asserts. */}
        <ReasonChips player={player} nextPick={nextPick} max={1} />
        {top && !readOnly && (
          <span className="flex shrink-0 gap-1">
            <Button onClick={() => onDraft(player)} tone="primary" title="Draft to my team">
              <UserPlus size={12} /> Me
            </Button>
            <Button onClick={() => onTaken(player)} tone="danger" title="Someone else took them">
              <XCircle size={12} /> Taken
            </Button>
          </span>
        )}
      </div>
    </div>
  );
}

/** Four states, because "is it still connected?" is the question you'd ask. */
function SyncChip({ sync, onDisconnect }) {
  if (!sync?.connected) {
    return <span className="rounded-md bg-white/5 px-2 py-0.5 text-slate-500">Manual</span>;
  }
  if (sync.status === "auth") {
    return (
      <span className="flex items-center gap-2">
        <span className="rounded-md bg-rose-500/15 px-2 py-0.5 text-rose-300">
          ESPN sign-in expired
        </span>
        <button
          onClick={onDisconnect}
          className="rounded-md bg-white/10 px-2 py-0.5 font-medium text-slate-200 hover:bg-white/15"
        >
          Draft manually
        </button>
      </span>
    );
  }
  if (sync.status === "stale") {
    return (
      <span className="rounded-md bg-amber-500/15 px-2 py-0.5 text-amber-300">
        Last synced {Math.round(sync.ageSeconds ?? 0)}s ago
      </span>
    );
  }
  return (
    <span className="flex items-center gap-1.5 rounded-md bg-emerald-500/15 px-2 py-0.5 text-emerald-300">
      <Radio size={10} /> ESPN live
      {sync.ageSeconds != null && (
        <span className="text-emerald-400/60">· {Math.round(sync.ageSeconds)}s</span>
      )}
    </span>
  );
}

/** What just happened, league-wide. Newest first, never wraps. */
function RecentPicksTicker({ draftLog }) {
  const recent = [...draftLog].slice(-8).reverse();
  if (!recent.length) return null;
  return (
    <div className="flex items-center gap-2 border-t border-white/5 bg-white/[0.02] px-4 py-1.5">
      <span className="label shrink-0">Just went</span>
      <div className="scroll-slim flex flex-1 gap-1.5 overflow-x-auto whitespace-nowrap">
        {recent.map((p) => (
          <span
            key={p.pick_number}
            className={`flex shrink-0 items-center gap-1.5 rounded-md px-2 py-0.5 text-[11px] ${
              p.is_my_pick
                ? "bg-emerald-500/10 text-emerald-200 ring-1 ring-emerald-400/25"
                : p.resolved === false
                ? "bg-rose-500/10 text-rose-200 ring-1 ring-rose-400/25"
                : "bg-white/[0.04] text-slate-300"
            }`}
            title={
              p.resolved === false
                ? "This player wasn't in the projection pool, so their position is unknown and no roster slot was filled."
                : undefined
            }
          >
            <span className="tabular text-slate-600">#{p.overall_pick ?? p.pick_number}</span>
            <span>{p.player_name || "Unknown"}</span>
            {p.position && <span className="text-slate-500">{p.position}</span>}
            {p.resolved === false && <span className="text-rose-300">?</span>}
          </span>
        ))}
      </div>
    </div>
  );
}

// ---- Sorting ----
// The board's own ranking is the default and the point of the tool, but a
// drafter often wants to interrogate it - "who's actually about to be gone",
// "who's the safest pick left" - which means sorting by the inputs rather than
// the verdict. `dir` is each column's *first* click direction: descending for
// quantities where bigger is better, ascending for ADP (pick 1 is best) and
// for names.
const SORT_COLUMNS = [
  { key: "player_name", label: "Player", align: "left", dir: "asc", cls: "" },
  { key: "position", label: "Pos", align: "left", dir: "asc", cls: "" },
  { key: "vorp", label: "VORP", align: "right", dir: "desc", cls: "hidden md:table-cell" },
  { key: "adp", label: "ADP", align: "right", dir: "asc", cls: "hidden md:table-cell" },
  { key: "availability", label: "Available", align: "right", dir: "desc", cls: "hidden sm:table-cell" },
  { key: "confidence", label: "Confidence", align: "right", dir: "desc", cls: "hidden sm:table-cell" },
  { key: "utility", label: "Score", align: "right", dir: "desc", cls: "" },
];

function SortHeader({ column, sort, onSort }) {
  const active = sort.key === column.key;
  const next = active
    ? { key: column.key, dir: sort.dir === "asc" ? "desc" : "asc" }
    : { key: column.key, dir: column.dir };

  return (
    <th
      className={`py-2.5 pr-3 text-[11px] font-medium uppercase tracking-wider ${
        column.align === "right" ? "text-right" : "text-left"
      } ${column.cls}`}
    >
      <button
        onClick={() => onSort(next)}
        title={`Sort by ${column.label}`}
        className={`inline-flex items-center gap-1 transition-colors hover:text-slate-300 ${
          active ? "text-slate-200" : ""
        }`}
      >
        {column.align === "right" && active && <SortArrow dir={sort.dir} />}
        {column.label}
        {column.align === "left" && active && <SortArrow dir={sort.dir} />}
      </button>
    </th>
  );
}

const SortArrow = ({ dir }) =>
  dir === "asc" ? (
    <ArrowUp size={11} className="text-emerald-400" />
  ) : (
    <ArrowDown size={11} className="text-emerald-400" />
  );

// ---- Risk dial ----
/**
 * The "Play it safe" slider, with its actual effect spelled out.
 *
 * On its own the control is invisible in its consequences: it scales value by
 * `(1 - aversion) + aversion x confidence`, and since most confidence values
 * sit in a narrow band the resulting multipliers differ by only a few percent
 * between skill-position players. The one place it bites hard is K and DST,
 * whose projections have historically explained ~none of the variance in what
 * those players delivered.
 *
 * So rather than leaving the user to infer that from a bare percentage, this
 * reads the per-player `risk_mult` the backend already returns and names the
 * positions currently being discounted most. A control whose effect can't be
 * seen may as well not exist.
 */
function RiskControl({ value, onChange, pool }) {
  const effect = useMemo(() => {
    const byPos = new Map();
    for (const p of pool) {
      const m = Number(p.risk_mult);
      if (!Number.isFinite(m) || !p.position) continue;
      const cur = byPos.get(p.position) || { sum: 0, n: 0 };
      byPos.set(p.position, { sum: cur.sum + (1 - m), n: cur.n + 1 });
    }
    return [...byPos.entries()]
      .map(([position, { sum, n }]) => ({ position, discount: sum / n }))
      .sort((a, b) => b.discount - a.discount);
  }, [pool]);

  const hardest = effect.filter((e) => e.discount > 0.005).slice(0, 3);
  const spread = effect.length ? effect[0].discount - effect[effect.length - 1].discount : 0;

  return (
    <div className="card px-4 py-3">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <span className="flex items-center gap-1.5 text-sm font-medium text-slate-300">
          <ShieldCheck size={14} className="text-slate-500" /> Play it safe
        </span>
        <input
          type="range" min={0} max={0.5} step={0.05}
          value={value}
          onChange={(e) => onChange(Number(e.target.value))}
          className="h-1 w-40 cursor-pointer accent-emerald-400"
        />
        <span className="tabular w-9 text-xs font-semibold text-slate-300">
          {Math.round(value * 100)}%
        </span>
        <span className="text-xs text-slate-500">
          {value === 0
            ? "Off — ranking on value, need and timing only."
            : `Cuts up to ${Math.round(value * 100)}% off players whose projections historically miss most.`}
        </span>
      </div>

      {value > 0 && hardest.length > 0 && (
        <div className="mt-2.5 flex flex-wrap items-center gap-x-2 gap-y-1.5 border-t border-white/5 pt-2.5">
          <span className="label">Currently discounting</span>
          {hardest.map((e) => (
            <span
              key={e.position}
              className="flex items-center gap-1 rounded-md bg-white/[0.04] px-2 py-0.5 text-xs"
              title={`Average value discount applied to ${e.position} right now.`}
            >
              <PositionChip position={e.position} />
              <span className="tabular text-rose-300">−{Math.round(e.discount * 100)}%</span>
            </span>
          ))}
          {spread < 0.03 && (
            <span className="text-[11px] text-slate-500">
              — spread is under 3%, so this is only breaking near-ties right now.
            </span>
          )}
        </div>
      )}
    </div>
  );
}

// ---- Roster panel ----
function RosterPanel({ rosterState, depth, picksRemaining, benchSlots, benchFilled }) {
  const entries = Object.entries(rosterState);
  const openCount = entries.reduce((n, [, v]) => n + Math.max(v.need - v.have, 0), 0);
  const tight = picksRemaining != null && openCount > 0 && picksRemaining <= openCount;
  const benchOpen = benchSlots != null ? Math.max(benchSlots - (benchFilled || 0), 0) : null;

  return (
    <div className="card p-4">
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-slate-200">My roster</h2>
        {picksRemaining != null && (
          <span className="tabular text-xs text-slate-500">{picksRemaining} picks left</span>
        )}
      </div>

      {entries.length === 0 ? (
        <div className="text-xs text-slate-500">No roster data.</div>
      ) : (
        <ul className="space-y-1.5">
          {entries.map(([pos, v]) => {
            const open = Math.max(v.need - v.have, 0);
            const extra = depth?.[pos] || 0;
            return (
              <li
                key={pos}
                className={`flex items-center justify-between rounded-lg px-2.5 py-1.5 text-xs
                  ${open > 0 ? "bg-white/[0.04]" : "bg-transparent text-slate-500"}`}
              >
                <span className="flex items-center gap-2">
                  <span className={`font-semibold ${open > 0 ? "text-slate-200" : "text-slate-500"}`}>
                    {pos}
                  </span>
                  {extra > 0 && <span className="text-[10px] text-slate-600">+{extra} depth</span>}
                </span>
                <SlotPips have={v.have} need={v.need} position={pos} />
              </li>
            );
          })}
        </ul>
      )}

      {/* Bench is a third of a 16-round draft. Showing it as real capacity
          keeps those picks from reading as unaccounted-for. */}
      {benchSlots != null && benchSlots > 0 && (
        <div className="mt-3 border-t border-white/5 pt-3">
          <div className="flex items-center justify-between text-xs">
            <span className="flex items-center gap-2">
              <span className="font-semibold text-slate-300">Bench</span>
              <span
                className="text-[10px] text-slate-600"
                title="Once your starting lineup is full, the board values depth by how often each position actually misses games — so a backup RB outranks a second kicker."
              >
                depth picks
              </span>
            </span>
            <span className="tabular text-slate-500">
              {benchFilled || 0}/{benchSlots}
            </span>
          </div>
          <div className="mt-1.5 flex flex-wrap gap-1">
            {Array.from({ length: benchSlots }).map((_, i) => (
              <span
                key={i}
                className={`h-2 w-2 rounded-full ${
                  i < (benchFilled || 0) ? "bg-slate-400" : "bg-white/15"
                }`}
              />
            ))}
          </div>
          {benchOpen === 0 && (
            <div className="mt-2 text-[11px] leading-relaxed text-slate-500">
              Bench is full — every remaining pick has to upgrade a starter.
            </div>
          )}
        </div>
      )}

      {tight && (
        <div className="mt-3 rounded-lg border border-amber-400/25 bg-amber-500/10 px-2.5 py-2 text-[11px] leading-relaxed text-amber-300">
          {openCount} starting {openCount === 1 ? "slot" : "slots"} still open with {picksRemaining}{" "}
          {picksRemaining === 1 ? "pick" : "picks"} left — the board is now weighting need heavily
          over raw value.
        </div>
      )}
    </div>
  );
}

// ---- Draft log ----
const DraftLog = ({ draftLog }) => (
  <div className="card p-4">
    <h2 className="mb-3 text-sm font-semibold text-slate-200">Full draft log</h2>
    {draftLog.length === 0 ? (
      <div className="text-xs text-slate-500">No picks yet.</div>
    ) : (
      <ul className="scroll-slim max-h-64 space-y-0.5 overflow-y-auto pr-1">
        {[...draftLog].slice(-40).reverse().map((p) => (
          <li
            key={p.pick_number}
            className={`flex items-center gap-2 rounded-md px-2 py-1.5 text-xs
              ${p.is_my_pick ? "bg-emerald-500/10" : ""}`}
          >
            <span className="tabular w-7 shrink-0 text-slate-600">{p.pick_number}</span>
            <span className="flex-1 truncate text-slate-300">{p.player_name || "—"}</span>
            {p.filled_slot && p.filled_slot !== p.position && (
              <span className="shrink-0 text-[10px] uppercase text-teal-400">{p.filled_slot}</span>
            )}
            <span className="shrink-0 text-slate-600">{p.position}</span>
          </li>
        ))}
      </ul>
    )}
  </div>
);

// ---- Main ----
export default function DraftBoard() {
  const [mode, setMode] = useState("setup"); // "setup" | "live" | "offline"
  const [starting, setStarting] = useState(false);
  const [setupError, setSetupError] = useState(null);

  const [sessionId, setSessionId] = useState(null);
  const [teams, setTeams] = useState(14);
  const [mySlot, setMySlot] = useState(1);
  const [rounds, setRounds] = useState(DEFAULT_ROUNDS);

  const [pool, setPool] = useState([]);
  const [rosterState, setRosterState] = useState({});
  const [depth, setDepth] = useState({});
  const [picksRemaining, setPicksRemaining] = useState(null);
  const [benchSlots, setBenchSlots] = useState(null);
  const [benchFilled, setBenchFilled] = useState(0);
  const [draftLog, setDraftLog] = useState([]);

  const [posFilter, setPosFilter] = useState("ALL");
  // Split so typing stays instant: the input is bound to `queryInput`, while
  // the filter+sort over ~1,000 players runs off the debounced `query`. Shorter
  // than the risk slider's 250ms because that one guards a network round-trip
  // and this guards local work - a quarter second of lag on a search box during
  // a live draft is very noticeable.
  const [queryInput, setQueryInput] = useState("");
  const [query, setQuery] = useState("");
  const [analysisSeen, setAnalysisSeen] = useState(false);
  const [riskAversion, setRiskAversion] = useState(DEFAULT_RISK_AVERSION);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);
  const [showLegend, setShowLegend] = useState(false);
  // Both stored as ids, not player objects, so they keep showing fresh numbers
  // after a re-rank instead of a stale snapshot. `expandedId` is the inline
  // row; `selectedId` is the full side panel.
  const [selectedId, setSelectedId] = useState(null);
  const [expandedId, setExpandedId] = useState(null);
  const [tab, setTab] = useState("board"); // "board" | "analysis"
  // Live ESPN state. `connected` gates whether pick numbers come from the
  // league or from local snake math.
  const [sync, setSync] = useState({ connected: false, status: null, ageSeconds: null,
                                     version: -1, team: null });
  const [syncCtx, setSyncCtx] = useState(null);
  const [sort, setSort] = useState({ key: "utility", dir: "desc" });
  const [pickOrder, setPickOrder] = useState([]);
  const [teamsList, setTeamsList] = useState([]);
  const [leagues, setLeagues] = useState(null);      // null = still loading
  const [leaguesError, setLeaguesError] = useState(false);
  const [adpYear, setAdpYear] = useState(null);
  const [leagueId, setLeagueId] = useState(null);

  // Sign-in. "unknown" until the stored token has been checked with the
  // server, so a remembered device never flashes the sign-in screen on the way
  // to its board - and a revoked token still lands there rather than on a
  // board that can't load.
  const [authState, setAuthState] = useState(deviceToken ? "unknown" : "out");
  const [signingIn, setSigningIn] = useState(false);
  const [signInError, setSignInError] = useState(null);
  // Set while the remembered league is being reconnected, so the app shows a
  // restoring state instead of the picker it is about to skip.
  const [restoring, setRestoring] = useState(false);

  // Where the draft is. Two implementations, deliberately:
  //
  // When connected, this comes from ESPN's real pick order, which survives
  // traded picks, keepers and any draft that isn't a pure snake - none of
  // which the arithmetic below can express. The local version is a *guess*,
  // used only when there's no league to ask, and it is never consulted in
  // live mode.
  //
  // `nextPick` is the turn AFTER the one being decided, because the timing
  // question is always "grab them now, or will they last until I'm back?".
  const localCtx = useMemo(() => {
    const currentPick = draftLog.length + 1;
    const thisTurn = nextMyPick(currentPick, mySlot, teams);
    return {
      currentPick,
      thisTurn,
      nextPick: nextMyPick(thisTurn + 1, mySlot, teams),
      isMyTurn: slotForPick(currentPick, teams) === mySlot,
      picksUntilMyTurn: Math.max(thisTurn - currentPick, 0),
      teams,
    };
  }, [draftLog.length, mySlot, teams]);

  const pickCtx = sync.connected && syncCtx ? syncCtx : localCtx;
  const currentPick = pickCtx.currentPick;
  const nextPick = pickCtx.nextPick;
  const isMyTurn = pickCtx.isMyTurn;

  const refreshRecommendations = useCallback(
    async (sid, cp, np, risk) => {
      setLoading(true);
      setErr(null);
      try {
        const data = await apiPost("/recommend", {
          session_id: sid,
          current_pick: cp,
          next_pick: np,
          topn: RECOMMEND_TOPN,
          risk_aversion: risk,
        });
        setPool(data.results || []);
        if (data.roster_state) setRosterState(data.roster_state);
        if (data.picks_remaining !== undefined) setPicksRemaining(data.picks_remaining);
        if (data.depth) setDepth(data.depth);
        if (data.bench_slots !== undefined) setBenchSlots(data.bench_slots);
        if (data.bench_filled !== undefined) setBenchFilled(data.bench_filled);
        if (data.adp_year !== undefined) setAdpYear(data.adp_year);
      } catch (e) {
        setErr(String(e));
      } finally {
        setLoading(false);
      }
    },
    []
  );

  const loadOfflineFallback = useCallback(async () => {
    try {
      const res = await fetch(`/players.json?ts=${Date.now()}`, { cache: "no-store" });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const raw = await res.json();
      setPool(
        (raw || [])
          .map((r) => {
            const point = Number(r.predicted_vorp);
            const low = Number(r.ci_low);
            // Offline has the model interval but not the position-reliability
            // table, so confidence here is the model term only - narrower than
            // the live figure. Labelled as approximate in the offline banner.
            const conf =
              Number.isFinite(point) && point > 0 && Number.isFinite(low)
                ? Math.max(0, Math.min(1, low / point))
                : null;
            return {
              player_id: r.player_id,
              player_name: r.player_name,
              position: r.position,
              pro_team: r.pro_team,
              is_rookie: Number(r.is_rookie ?? 0),
              projected_points: Number(r.projected_points ?? 0),
              avg_last_year: Number(r.avg_last_year ?? 0),
              vorp: Number(r.proj_vorp ?? 0),
              vorp_z: Number(r.proj_vorp_z ?? r.proj_vorp ?? 0),
              predicted_vorp: Number.isFinite(point) ? point : null,
              ci_low: Number.isFinite(low) ? low : null,
              ci_high: Number.isFinite(Number(r.ci_high)) ? Number(r.ci_high) : null,
              adp: Number(r.adp ?? 999),
              confidence: conf,
              availability: null, // needs a live session's pick numbers
              // League timing is a live-only signal for the same reason: the
              // offline banner already says pick timing isn't applied, and a
              // shifted pick estimate with no availability to shift would be
              // stating a conclusion the fallback can't actually reach.
              bias_shift: null,
              bias_pos_shift: null,
              bias_team_shift: null,
              bias_reason: null,
              // Ranked on the position-dampened VORP, matching the live
              // backend's value term - offline can't apply need or timing,
              // which is exactly what the banner warns about.
              utility: Number(r.proj_vorp_z ?? r.proj_vorp ?? 0),
            };
          })
          .sort((a, b) => b.utility - a.utility)
      );
      setMode("offline");
    } catch (e) {
      setSetupError(`Backend unreachable and offline fallback failed: ${e}`);
    } finally {
      setStarting(false);
    }
  }, []);

  // --- live ESPN sync ---
  // Applies a payload from /espn/connect or /espn/sync. The server sends the
  // full authoritative state, not a delta, so everything is replaced wholesale
  // - which is what makes a missed poll or a page refresh self-healing.
  // Leagues are discovered from the credentials, so the home screen can list
  // them by name with their draft dates instead of asking for an id.
  //
  // Loaded once signed in rather than once on the setup screen, because the
  // top-bar league dropdown needs them on a device that restored straight to a
  // board and never saw setup.
  useEffect(() => {
    if (authState === "out") return;
    let cancelled = false;
    apiGet("/espn/leagues")
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((d) => !cancelled && setLeagues(d.leagues || []))
      .catch(() => !cancelled && setLeaguesError(true));
    return () => { cancelled = true; };
  }, [authState]);

  const absorbSync = useCallback((data) => {
    setSync({
      connected: data.connected !== false,
      status: data.status ?? "ok",
      message: data.message ?? null,
      ageSeconds: data.age_seconds ?? null,
      version: data.version ?? 0,
      team: data.team ?? null,
      league: data.league ?? null,
      complete: !!data.complete,
    });
    setSyncCtx({
      currentPick: data.current_pick,
      thisTurn: data.this_turn,
      nextPick: data.next_pick,
      isMyTurn: !!data.is_my_turn,
      picksUntilMyTurn: data.picks_until_my_turn,
      myPicks: data.my_picks ?? [],
      teams: data.teams ?? teams,
    });
    if (data.draft_log) setDraftLog(data.draft_log);
    if (data.roster_state) setRosterState(data.roster_state);
    if (data.depth) setDepth(data.depth);
    if (data.picks_remaining !== undefined) setPicksRemaining(data.picks_remaining);
    if (data.bench_slots !== undefined) setBenchSlots(data.bench_slots);
    if (data.bench_filled !== undefined) setBenchFilled(data.bench_filled);
  }, [teams]);

  const connectEspn = useCallback(async (sid, leagueId) => {
    const data = await apiPost("/espn/connect", { session_id: sid, league_id: leagueId });
    absorbSync(data);
    if (data.teams) setTeams(data.teams);
    if (data.rounds) setRounds(data.rounds);
    // The full order and the team names only come back on connect - they don't
    // change during a draft, so there's no reason to resend them every poll.
    if (data.pick_order) setPickOrder(data.pick_order);
    if (data.teams_list) setTeamsList(data.teams_list);
    return data;
  }, [absorbSync]);

  const disconnectEspn = useCallback(async () => {
    if (sessionId) {
      try {
        await apiPost("/espn/disconnect", { session_id: sessionId });
      } catch { /* the escape hatch must work even if the call fails */ }
    }
    setSync((s) => ({ ...s, connected: false, status: null }));
    setSyncCtx(null);
  }, [sessionId]);

  // Poll while connected. Stops when the tab is hidden (state fully recovers on
  // the next poll), when the draft finishes, and after an auth failure -
  // retrying with dead cookies accomplishes nothing and risks rate limiting.
  useEffect(() => {
    if (!sync.connected || !sessionId) return;
    if (sync.status === "auth" || sync.complete) return;
    let cancelled = false;

    const tick = async () => {
      if (document.hidden) return;
      try {
        const res = await fetch(`${API_URL}/espn/sync/${sessionId}`);
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();
        if (cancelled) return;
        const changed = data.version !== sync.version;
        absorbSync(data);
        // Only re-rank when something actually happened. Most polls are a
        // passthrough of an unchanged snapshot.
        if (changed && data.current_pick != null) {
          refreshRecommendations(sessionId, data.current_pick, data.next_pick, riskAversion);
        }
      } catch {
        if (!cancelled) setSync((s) => ({ ...s, status: "stale" }));
      }
    };

    const id = setInterval(tick, 5000);
    return () => { cancelled = true; clearInterval(id); };
  }, [sync.connected, sync.status, sync.complete, sync.version, sessionId,
      absorbSync, refreshRecommendations, riskAversion]);

  const handleStart = useCallback(
    async ({ espn, leagueId, teams: t = 14, mySlot: s = 1, rounds: r = DEFAULT_ROUNDS }) => {
      setStarting(true);
      setSetupError(null);
      setTeams(t);
      setMySlot(s);
      setRounds(r);
      let session_id;
      try {
        ({ session_id } = await apiPost("/session", {
          teams: t,
          roster_need: DEFAULT_ROSTER_NEED,
          rounds: r,
        }));
      } catch {
        // No backend at all - the read-only snapshot is the only option.
        await loadOfflineFallback();
        return;
      }

      setSessionId(session_id);
      setDraftLog([]);
      setPicksRemaining(r);
      setRosterState(
        Object.fromEntries(Object.entries(DEFAULT_ROSTER_NEED).map(([k, v]) => [k, { have: 0, need: v }]))
      );

      if (espn) {
        try {
          // Connect *before* showing the board: it supplies the real team
          // count, round count and pick order, and starting on guessed values
          // would mean the first recommendation used the wrong next_pick.
          const data = await connectEspn(session_id, leagueId);
          const settled = data.league?.id ?? leagueId ?? null;
          setLeagueId(settled);
          // Remembered only after a successful connect, so a league that fails
          // to load can't strand the next visit on a broken restore.
          if (settled) saveDevice({ lastLeagueId: String(settled) });
          setMode("live");
          setStarting(false);
          await refreshRecommendations(
            session_id, data.current_pick ?? 1, data.next_pick ?? 2, riskAversion
          );
          return;
        } catch (e) {
          // Stay on the setup screen with the manual path still available.
          setSetupError(
            e.status === 502 || e.status === 503
              ? e.message || "Couldn't reach your ESPN league. Try signing in again, or set up manually."
              : String(e.message || e)
          );
          setStarting(false);
          return;
        }
      }

      setMode("live");
      setStarting(false);
      const thisTurn = nextMyPick(1, s, t);
      await refreshRecommendations(session_id, 1, nextMyPick(thisTurn + 1, s, t), riskAversion);
    },
    [refreshRecommendations, loadOfflineFallback, connectEspn, riskAversion]
  );

  // --- sign in, and staying signed in ---

  const handleSignIn = useCallback(async (swid, espn_s2) => {
    setSigningIn(true);
    setSignInError(null);
    try {
      const data = await apiPost("/auth/connect", { swid, espn_s2 });
      deviceToken = data.token;
      saveDevice({ token: data.token });
      setLeagues(data.leagues || []);
      setAuthState("in");
    } catch (e) {
      setSignInError(e.message || String(e));
    } finally {
      setSigningIn(false);
    }
  }, []);

  const handleSignOut = useCallback(async () => {
    try {
      await apiPost("/auth/forget");
    } catch { /* forgetting locally is what matters; do it either way */ }
    deviceToken = null;
    saveDevice({ token: null, lastLeagueId: null });
    setAuthState("out");
    setMode("setup");
    setLeagues(null);
    setLeagueId(null);
    setSessionId(null);
    setSync({ connected: false, status: null, ageSeconds: null, version: -1, team: null });
    setSyncCtx(null);
  }, []);

  // Validate a remembered token before trusting it. Cookies expire and the
  // store can be cleared server-side, and finding that out here - rather than
  // as a board that mysteriously won't load - is the difference between "sign
  // in again" and a bug report.
  useEffect(() => {
    if (authState !== "unknown") return;
    let cancelled = false;
    apiGet("/auth/session")
      .then((r) => {
        if (cancelled) return;
        if (r.ok) return setAuthState("in");
        deviceToken = null;
        saveDevice({ token: null });
        setAuthState("out");
      })
      .catch(() => {
        // Backend down, not signed out. Keep the token and let the setup
        // screen's own offline fallback handle it.
        if (!cancelled) setAuthState("in");
      });
    return () => { cancelled = true; };
  }, [authState]);

  // Reopening the app lands on the last league's board rather than the picker.
  // Runs once, only with a league to restore, and gives up quietly to the
  // picker if that league is gone from the account.
  const restored = useRef(false);
  useEffect(() => {
    if (authState !== "in" || mode !== "setup" || restored.current) return;
    const last = loadDevice().lastLeagueId;
    if (!last || leagues === null) return;   // wait for the list
    restored.current = true;
    if (!leagues.some((l) => String(l.league_id) === String(last))) {
      saveDevice({ lastLeagueId: null });
      return;
    }
    setRestoring(true);
    handleStart({ espn: true, leagueId: String(last) }).finally(() => setRestoring(false));
  }, [authState, mode, leagues, handleStart]);

  // Switching leagues from the top bar. A fresh session, because team count,
  // rounds and roster need all belong to the league - reusing the old one
  // would carry the previous league's shape into the new board.
  const switchLeague = useCallback(
    (id) => {
      if (String(id) === String(leagueId)) return;
      setPool([]);
      setDraftLog([]);
      setPickOrder([]);
      setTeamsList([]);
      setSyncCtx(null);
      setSync({ connected: false, status: null, ageSeconds: null, version: -1, team: null });
      setTab("board");
      handleStart({ espn: true, leagueId: String(id) });
    },
    [leagueId, handleStart]
  );

  const submitPick = useCallback(
    async (player, isMyPick) => {
      // The player is leaving the board either way, so close their panel/row.
      setSelectedId((id) => (id === player.player_id ? null : id));
      setExpandedId((id) => (id === player.player_id ? null : id));
      if (mode === "offline") {
        setPool((list) => list.filter((p) => p.player_id !== player.player_id));
        return;
      }
      setErr(null);
      try {
        const data = await apiPost("/pick", {
          session_id: sessionId,
          player_id: player.player_id,
          position: player.position,
          player_name: player.player_name,
          is_my_pick: isMyPick,
        });
        setRosterState(data.roster_state);
        setDepth(data.depth || {});
        setPicksRemaining(data.picks_remaining);
        setBenchSlots(data.bench_slots ?? null);
        setBenchFilled(data.bench_filled ?? 0);
        setDraftLog(data.draft_log);
        const cp = data.draft_log.length + 1;
        const thisTurn = nextMyPick(cp, mySlot, teams);
        await refreshRecommendations(sessionId, cp, nextMyPick(thisTurn + 1, mySlot, teams), riskAversion);
      } catch (e) {
        setErr(String(e));
      }
    },
    [mode, sessionId, mySlot, teams, refreshRecommendations, riskAversion]
  );

  // Re-rank when the risk dial moves. Debounced so dragging the slider doesn't
  // fire a request per pixel.
  useEffect(() => {
    if (mode !== "live" || !sessionId) return;
    const t = setTimeout(() => {
      refreshRecommendations(sessionId, currentPick, nextPick, riskAversion);
    }, 250);
    return () => clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [riskAversion]);

  useEffect(() => {
    const t = setTimeout(() => setQuery(queryInput), 150);
    return () => clearTimeout(t);
  }, [queryInput]);

  // Identity-stable row handlers. They take the player (or id) as an argument
  // rather than closing over it, which is what lets one function serve every
  // row and lets memoised rows skip re-rendering when an unrelated one changes.
  const handleToggle = useCallback(
    (id) => setExpandedId((cur) => (cur === id ? null : id)),
    []
  );
  const handleOpenFull = useCallback((id) => setSelectedId(id), []);
  const handleDraft = useCallback((p) => submitPick(p, true), [submitPick]);
  const handleTaken = useCallback((p) => submitPick(p, false), [submitPick]);

  // The title is the way home. Mid-draft it confirms first: a stray click that
  // discards a part-built roster isn't recoverable, and the drafted players
  // only live in server memory.
  const goHome = useCallback(() => {
    if (draftLog.length > 0 &&
        !window.confirm("Leave this draft and pick a different league? Your picks here will be cleared.")) {
      return;
    }
    disconnectEspn();
    setPool([]);
    setDraftLog([]);
    setExpandedId(null);
    setSelectedId(null);
    setPickOrder([]);
    setTeamsList([]);
    setLeagues(null);
    setLeaguesError(false);
    setSetupError(null);
    setMode("setup");
  }, [draftLog.length, disconnectEspn]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    const rows = pool
      .filter((p) => (posFilter === "ALL" ? true : p.position === posFilter))
      .filter((p) => !q || (p.player_name || "").toLowerCase().includes(q));

    const { key, dir } = sort;
    const sign = dir === "asc" ? 1 : -1;
    // Missing values always sort last regardless of direction - a player with
    // no ADP isn't "the earliest pick", and a null availability isn't "least
    // likely to last". Sorting them to the bottom keeps the top of the board
    // meaningful whichever column is active.
    const cmp = (a, b) => {
      const av = key === "vorp" ? (a.vorp_z ?? a.vorp) : a[key];
      const bv = key === "vorp" ? (b.vorp_z ?? b.vorp) : b[key];
      const aMissing = av == null || (key === "adp" && Number(av) >= NO_ADP);
      const bMissing = bv == null || (key === "adp" && Number(bv) >= NO_ADP);
      if (aMissing || bMissing) return aMissing - bMissing;
      if (typeof av === "string" || typeof bv === "string") {
        return sign * String(av).localeCompare(String(bv));
      }
      const d = Number(av) - Number(bv);
      // Score breaks every tie, so equal-VORP or equal-position rows still
      // come back in the board's own order rather than an arbitrary one.
      return d !== 0 ? sign * d : (b.utility ?? 0) - (a.utility ?? 0);
    };
    return [...rows].sort(cmp);
  }, [pool, posFilter, query, sort]);

  // The board's own verdict, independent of how the table happens to be
  // sorted: sorting by ADP must never relabel the earliest-drafted player as
  // "best available for you", which is precisely the claim the tool exists to
  // argue against. The list below shows every player including these.
  const topThree = useMemo(
    () => [...filtered].sort((a, b) => (b.utility ?? 0) - (a.utility ?? 0)).slice(0, 3),
    [filtered]
  );
  const topId = topThree[0]?.player_id ?? null;
  const customSort = sort.key !== "utility" || sort.dir !== "desc";

  // Open a player's row and bring it into view. Used by the top-three pills,
  // which are pointers into the list rather than a separate surface.
  const focusPlayer = useCallback((id) => {
    setExpandedId(id);
    requestAnimationFrame(() => {
      document.getElementById(`row-${id}`)?.scrollIntoView({ block: "center", behavior: "smooth" });
    });
  }, []);
  // Read-only offline, and while ESPN is driving: manual clicks would be
  // silently reverted by the next sync, so the buttons say so by going away.
  // Disconnecting brings them straight back.
  const readOnly = mode === "offline" || (sync.connected && sync.status !== "auth");
  // Resolved from the live pool each render, so an open panel picks up new
  // availability/score numbers when the board re-ranks behind it.
  const selectedPlayer = useMemo(
    () => (selectedId == null ? null : pool.find((p) => p.player_id === selectedId) || null),
    [pool, selectedId]
  );

  // A remembered token is being checked, or its league reconnected. Both are a
  // round-trip; showing sign-in or the picker underneath would flash a screen
  // the user is not going to end up on.
  if (authState === "unknown" || restoring) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <div className="flex items-center gap-2 text-sm text-slate-500">
          <Trophy className="text-emerald-400" size={16} />
          {restoring ? "Loading your league…" : "Signing you in…"}
        </div>
      </div>
    );
  }

  if (authState === "out") {
    return <SignInScreen onSignIn={handleSignIn} busy={signingIn} error={signInError} />;
  }

  if (mode === "setup") {
    return (
      <SetupScreen
        onStart={handleStart}
        starting={starting}
        error={setupError}
        leagues={leagues}
        leaguesError={leaguesError}
        onSignOut={handleSignOut}
      />
    );
  }

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-20 border-b border-white/5 bg-slate-950/85 backdrop-blur">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-3 px-4 py-3">
          <button
            onClick={goHome}
            title="Back to your leagues"
            className="flex items-center gap-2 rounded-lg px-1.5 py-1 transition hover:bg-white/5"
          >
            <Trophy className="text-emerald-400" size={18} />
            <span className="font-semibold tracking-tight">Justin's Draft Assistant</span>
          </button>

          {mode !== "offline" && (
            <LeagueMenu
              leagues={leagues}
              currentId={leagueId}
              currentName={sync.league?.name}
              onSwitch={switchLeague}
              onSignOut={handleSignOut}
            />
          )}

          <div className="flex items-center gap-1 rounded-lg border border-white/10 bg-slate-900 p-1">
            {[
              { id: "board", label: "Draft board", icon: <LayoutList size={12} /> },
              ...(sync.connected
                ? [{ id: "draftday", label: "Draft day", icon: <CalendarClock size={12} /> }]
                : []),
              { id: "analysis", label: "Analysis", icon: <BarChart3 size={12} /> },
            ].map((t) => (
              <button
                key={t.id}
                onClick={() => {
                  if (t.id === "analysis") setAnalysisSeen(true);
                  setTab(t.id);
                }}
                className={`flex items-center gap-1.5 rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                  tab === t.id ? "bg-white/10 text-white" : "text-slate-500 hover:text-slate-300"
                }`}
              >
                {t.icon} {t.label}
              </button>
            ))}
          </div>

          {/* Pick and turn live in DraftStatusStrip now, which says the same
              things plus the round and how many picks away you are - and reads
              them from the league rather than from local snake math. */}
          {tab === "board" && mode === "offline" && (
            <Badge tone="warn" title="The backend isn't running, so need and timing can't be applied.">
              <WifiOff size={11} /> Offline — value only
            </Badge>
          )}

          <div className="ml-auto flex items-center gap-2">
            {tab === "board" && loading && <span className="text-xs text-slate-500">Updating…</span>}
            {tab === "board" && err && (
              <span className="max-w-[16rem] truncate text-xs text-rose-400">{err}</span>
            )}
            {tab === "board" && (
              <>
                <Button onClick={() => setShowLegend((v) => !v)} title="What do these columns mean?">
                  <Info size={12} /> Explain
                  <ChevronDown size={12} className={showLegend ? "rotate-180 transition" : "transition"} />
                </Button>
                <Button onClick={() => window.location.reload()} title="Reset board">
                  <RefreshCw size={12} /> Reset
                </Button>
              </>
            )}
          </div>
        </div>

        {showLegend && (
          <div className="border-t border-white/5 bg-slate-900/60">
            <dl className="mx-auto grid max-w-7xl gap-4 px-4 py-5 sm:grid-cols-2 lg:grid-cols-3">
              {LEGEND.map((item) => (
                <div key={item.term}>
                  <dt className="mb-1 text-sm font-semibold text-slate-200">{item.term}</dt>
                  <dd className="text-xs leading-relaxed text-slate-400">{item.body}</dd>
                </div>
              ))}
            </dl>
          </div>
        )}
      </header>

      {/* Mounted on first visit and kept mounted thereafter, hidden rather than
          unmounted. Unmounting threw away the fetched payload, so every switch
          back paid the full request again. `hidden` is enough here because the
          wrapper carries no display utility class that would override it. */}
      {tab === "draftday" && sync.connected && (
        <DraftDay
          league={sync.league}
          pickCtx={pickCtx}
          order={pickOrder}
          teamsList={teamsList}
          rosterState={rosterState}
          picksRemaining={picksRemaining}
          myTeamId={sync.team?.id}
        />
      )}

      {analysisSeen && (
        <div hidden={tab !== "analysis"}>
          <Suspense fallback={<div className="mx-auto max-w-5xl p-8 text-sm text-slate-500">Loading analysis…</div>}>
            {/* The session is how the analysis learns the league's lineup, and
                the lineup sets replacement level - so the numbers on this page
                are the ones this league would actually see. */}
            <Analysis
              apiUrl={API_URL}
              leagueId={leagueId}
              leagueName={sync.league?.name}
              sessionId={sessionId}
              authHeaders={authHeaders()}
            />
          </Suspense>
        </div>
      )}

      {tab === "board" && (
      <main className="mx-auto grid max-w-7xl grid-cols-1 gap-5 p-4 lg:grid-cols-[minmax(0,1fr)_300px]">
        <div className="min-w-0 space-y-4">
          {mode === "offline" && (
            <div className="rounded-xl border border-amber-400/25 bg-amber-500/10 px-4 py-3 text-xs leading-relaxed text-amber-200">
              The backend isn't reachable, so this is the last exported ranking: <strong>value
              only</strong>. Roster need, pick timing and the position-reliability half of
              confidence all need a live session, and aren't applied here.
            </div>
          )}

          {/* Three one-line strips instead of a hero card. The board is the
              point of this screen, so the chrome above it has to earn its
              height - together these take less room than the single card did
              and say considerably more. */}
          <BoardHeader
            mode={mode}
            pickCtx={pickCtx}
            sync={sync}
            onDisconnect={disconnectEspn}
            players={topThree}
            nextPick={nextPick}
            readOnly={readOnly}
            customSort={customSort}
            onOpen={focusPlayer}
            onDraft={handleDraft}
            onTaken={handleTaken}
            draftLog={draftLog}
          />

          <div className="flex flex-wrap items-center gap-2">
            <div className="relative min-w-[200px] flex-1 sm:max-w-xs">
              <Search className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-600" size={14} />
              <input
                value={queryInput}
                onChange={(e) => setQueryInput(e.target.value)}
                placeholder="Search player…"
                className="h-9 w-full rounded-lg border border-white/10 bg-slate-900 pl-9 pr-3 text-sm
                  text-slate-100 outline-none transition placeholder:text-slate-600
                  focus:border-emerald-400/50 focus:ring-2 focus:ring-emerald-400/20"
              />
            </div>

            <div className="flex items-center gap-1 rounded-lg border border-white/10 bg-slate-900 p-1">
              {POSITIONS.map((p) => (
                <button
                  key={p}
                  onClick={() => setPosFilter(p)}
                  className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${
                    posFilter === p
                      ? "bg-white/10 text-white"
                      : "text-slate-500 hover:text-slate-300"
                  }`}
                >
                  {p}
                </button>
              ))}
            </div>

          </div>

          {/* When a custom sort is active the table is no longer the board's
              ranking, and silently letting it look like one would undo the
              whole point of the score column. */}
          {customSort && (
            <div className="flex items-center gap-2 rounded-lg border border-sky-400/20 bg-sky-500/5 px-3 py-2 text-xs text-sky-200/90">
              <ArrowUpDown size={13} className="shrink-0 text-sky-400" />
              <span>
                Sorted by{" "}
                <strong>{SORT_COLUMNS.find((c) => c.key === sort.key)?.label ?? sort.key}</strong> —
                this isn't the board's recommended order.
              </span>
              <button
                onClick={() => setSort({ key: "utility", dir: "desc" })}
                className="ml-auto shrink-0 rounded-md bg-white/10 px-2 py-0.5 font-medium text-sky-100 transition hover:bg-white/15"
              >
                Back to ranking
              </button>
            </div>
          )}

          <div className="card overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full border-collapse text-sm">
                <thead>
                  <tr className="bg-white/[0.03] text-slate-500">
                    <th className="w-10 py-2.5 pl-4 pr-2 text-right text-[11px] font-medium uppercase tracking-wider">#</th>
                    {SORT_COLUMNS.map((c) => (
                      <SortHeader
                        key={c.key}
                        column={c}
                        sort={sort}
                        onSort={setSort}
                      />
                    ))}
                    <th className="py-2.5 pl-2 pr-4" />
                  </tr>
                </thead>
                <tbody>
                  {/* Handlers are stable across renders (see the useCallbacks
                      above) so React.memo on PlayerRow can actually skip work.
                      Passing fresh arrows here would defeat it entirely. */}
                  {filtered.map((p, i) => (
                    <PlayerRow
                      key={p.player_id}
                      player={p}
                      rank={i + 1}
                      // Marks the board's pick wherever it lands in the current
                      // sort - which the old hero card couldn't do, because it
                      // sat outside the list entirely.
                      highlight={p.player_id === topId}
                      nextPick={nextPick}
                      readOnly={readOnly}
                      expanded={expandedId === p.player_id}
                      onToggle={handleToggle}
                      onOpenFull={handleOpenFull}
                      onDraft={handleDraft}
                      onTaken={handleTaken}
                    />
                  ))}
                  {!loading && filtered.length === 0 && (
                    <tr>
                      <td colSpan={9} className="py-12 text-center text-sm text-slate-500">
                        No players match your filters.
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>

        <aside className="space-y-4 lg:sticky lg:top-[60px] lg:self-start">
          {mode === "live" && (
            <RiskControl value={riskAversion} onChange={setRiskAversion} pool={pool} />
          )}
          <RosterPanel
            rosterState={rosterState}
            depth={depth}
            picksRemaining={picksRemaining}
            benchSlots={benchSlots}
            benchFilled={benchFilled}
          />
          <DraftLog draftLog={draftLog} />
        </aside>
      </main>
      )}

      <PlayerDetail
        player={selectedPlayer}
        nextPick={nextPick}
        adpYear={adpYear}
        readOnly={readOnly}
        onClose={() => setSelectedId(null)}
        onDraft={() => submitPick(selectedPlayer, true)}
        onTaken={() => submitPick(selectedPlayer, false)}
      />
    </div>
  );
}
