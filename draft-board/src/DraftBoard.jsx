import React, { useCallback, useEffect, useMemo, useState } from "react";
import {
  ChevronDown,
  Info,
  RefreshCw,
  Search,
  ShieldCheck,
  Sparkles,
  Timer,
  Trophy,
  UserPlus,
  WifiOff,
  XCircle,
} from "lucide-react";

import { LEGEND, RangeText, ReasonChips } from "./components/explain";
import PlayerDetail from "./components/PlayerDetail";
import { Badge, Button, Meter, PositionChip, SlotPips } from "./components/primitives";
import { confidenceBand, fmt, fmtAdp, pct, urgencyBand } from "./theme";

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

async function apiPost(path, body) {
  const res = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  return res.json();
}

// ---- Session setup ----
function SetupScreen({ onStart, starting, error }) {
  const [teams, setTeams] = useState(14);
  const [mySlot, setMySlot] = useState(1);
  const [rounds, setRounds] = useState(DEFAULT_ROUNDS);

  const field =
    "mt-1.5 h-10 w-full rounded-lg border border-white/10 bg-slate-900 px-3 text-sm text-slate-100 " +
    "outline-none transition focus:border-emerald-400/50 focus:ring-2 focus:ring-emerald-400/20";

  return (
    <div className="flex min-h-screen items-center justify-center p-6">
      <div className="card w-full max-w-md p-7">
        <div className="mb-1 flex items-center gap-2">
          <Trophy className="text-emerald-400" size={20} />
          <h1 className="text-xl font-semibold tracking-tight">Draft Assistant</h1>
        </div>
        <p className="mb-6 text-sm leading-relaxed text-slate-400">
          Live recommendations that account for your open roster slots, how long a player will
          last, and how much the projection can be trusted.
        </p>

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
        </div>

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

        <p className="mt-4 text-xs leading-relaxed text-slate-500">
          If the backend at <code className="text-slate-400">{API_URL}</code> isn't reachable, this
          falls back to a read-only board from the last exported rankings.
        </p>
      </div>
    </div>
  );
}

// ---- Top recommendation ----
function TopPickCard({ player, nextPick, onDraft, onTaken, onOpen, readOnly }) {
  if (!player) return null;
  const urgency = urgencyBand(player.availability);
  const conf = confidenceBand(player.confidence);
  const isRookie = Number(player.is_rookie) === 1;

  return (
    <div className="card relative overflow-hidden p-5">
      <div className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-emerald-400/60 to-transparent" />
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <div className="label mb-2 flex items-center gap-1.5 text-emerald-400/80">
            <Trophy size={12} /> Best available for you
          </div>
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
            {/* Not truncated: the player's name is the single most important
                thing on this card, and "Malik Na…" is useless at a glance. */}
            <button
              onClick={onOpen}
              className="text-left text-2xl font-semibold leading-tight tracking-tight
                transition hover:text-emerald-300"
              title="See full player detail"
            >
              {player.player_name}
            </button>
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

        {!readOnly && (
          <div className="flex gap-2">
            <Button onClick={onDraft} tone="solid" size="md" title="Draft to my team">
              <UserPlus size={14} /> Draft
            </Button>
            <Button onClick={onTaken} tone="danger" size="md" title="Someone else took them">
              <XCircle size={14} /> Taken
            </Button>
          </div>
        )}
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
function PlayerRow({ player, rank, nextPick, onDraft, onTaken, onOpen, readOnly }) {
  const urgency = urgencyBand(player.availability);
  const conf = confidenceBand(player.confidence);
  const isRookie = Number(player.is_rookie) === 1;

  // The whole row opens the detail panel, but the Draft/Taken buttons live
  // inside it - stopPropagation on the actions cell keeps a draft click from
  // also popping the panel open.
  return (
    <tr
      onClick={onOpen}
      className="group cursor-pointer border-t border-white/5 transition-colors hover:bg-white/[0.03]"
    >
      <td className="py-2.5 pl-4 pr-2 text-right">
        <span className="tabular text-xs text-slate-600">{rank}</span>
      </td>
      <td className="py-2.5 pr-3">
        <div className="flex items-center gap-2">
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
        {!readOnly && (
          <div className="flex justify-end gap-1 opacity-60 transition-opacity group-hover:opacity-100">
            <Button onClick={onDraft} tone="primary" title="Draft to my team">
              <UserPlus size={12} /> Me
            </Button>
            <Button onClick={onTaken} tone="danger" title="Someone else took them">
              <XCircle size={12} /> Taken
            </Button>
          </div>
        )}
      </td>
    </tr>
  );
}

// ---- Roster panel ----
function RosterPanel({ rosterState, depth, picksRemaining }) {
  const entries = Object.entries(rosterState);
  const openCount = entries.reduce((n, [, v]) => n + Math.max(v.need - v.have, 0), 0);
  const tight = picksRemaining != null && openCount > 0 && picksRemaining <= openCount;

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
    <h2 className="mb-3 text-sm font-semibold text-slate-200">Draft log</h2>
    {draftLog.length === 0 ? (
      <div className="text-xs text-slate-500">No picks yet.</div>
    ) : (
      <ul className="scroll-slim max-h-80 space-y-0.5 overflow-y-auto pr-1">
        {[...draftLog].reverse().map((p) => (
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
  const [draftLog, setDraftLog] = useState([]);

  const [posFilter, setPosFilter] = useState("ALL");
  const [query, setQuery] = useState("");
  const [riskAversion, setRiskAversion] = useState(DEFAULT_RISK_AVERSION);
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);
  const [showLegend, setShowLegend] = useState(false);
  // Stored as an id, not the player object, so the panel keeps showing fresh
  // numbers after a re-rank instead of a stale snapshot.
  const [selectedId, setSelectedId] = useState(null);
  const [adpYear, setAdpYear] = useState(null);

  const picksMade = draftLog.length;
  const currentPick = picksMade + 1;
  const isMyTurn = slotForPick(currentPick, teams) === mySlot;

  // The pick-timing question is always "grab them now, or will they last until
  // I'm back?" - so the reference point must be the turn AFTER the one being
  // decided. Passing the current pick when it's your turn (as this used to)
  // made the comparison degenerate exactly when it mattered most.
  const nextPick = useMemo(() => {
    const thisTurn = nextMyPick(currentPick, mySlot, teams);
    return nextMyPick(thisTurn + 1, mySlot, teams);
  }, [currentPick, mySlot, teams]);

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

  const handleStart = useCallback(
    async ({ teams: t, mySlot: s, rounds: r }) => {
      setStarting(true);
      setSetupError(null);
      setTeams(t);
      setMySlot(s);
      setRounds(r);
      try {
        const { session_id } = await apiPost("/session", {
          teams: t,
          roster_need: DEFAULT_ROSTER_NEED,
          rounds: r,
        });
        setSessionId(session_id);
        setDraftLog([]);
        setPicksRemaining(r);
        setRosterState(
          Object.fromEntries(Object.entries(DEFAULT_ROSTER_NEED).map(([k, v]) => [k, { have: 0, need: v }]))
        );
        setMode("live");
        setStarting(false);
        const thisTurn = nextMyPick(1, s, t);
        await refreshRecommendations(session_id, 1, nextMyPick(thisTurn + 1, s, t), riskAversion);
      } catch (e) {
        await loadOfflineFallback();
      }
    },
    [refreshRecommendations, loadOfflineFallback, riskAversion]
  );

  const submitPick = useCallback(
    async (player, isMyPick) => {
      // The player is leaving the board either way, so close their panel.
      setSelectedId((id) => (id === player.player_id ? null : id));
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

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return pool
      .filter((p) => (posFilter === "ALL" ? true : p.position === posFilter))
      .filter((p) => !q || (p.player_name || "").toLowerCase().includes(q));
  }, [pool, posFilter, query]);

  const topPick = filtered[0];
  const rest = filtered.slice(1);
  const readOnly = mode === "offline";
  // Resolved from the live pool each render, so an open panel picks up new
  // availability/score numbers when the board re-ranks behind it.
  const selectedPlayer = useMemo(
    () => (selectedId == null ? null : pool.find((p) => p.player_id === selectedId) || null),
    [pool, selectedId]
  );

  if (mode === "setup") {
    return <SetupScreen onStart={handleStart} starting={starting} error={setupError} />;
  }

  return (
    <div className="min-h-screen">
      <header className="sticky top-0 z-20 border-b border-white/5 bg-slate-950/85 backdrop-blur">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-3 px-4 py-3">
          <div className="flex items-center gap-2">
            <Trophy className="text-emerald-400" size={18} />
            <span className="font-semibold tracking-tight">Draft Assistant</span>
          </div>

          {mode === "live" && (
            <div className="flex items-center gap-2 text-xs">
              <span className="rounded-lg bg-white/5 px-2.5 py-1 text-slate-400">
                Pick <span className="tabular font-semibold text-slate-200">{currentPick}</span>
              </span>
              {isMyTurn ? (
                <Badge tone="calm">You're on the clock</Badge>
              ) : (
                <span className="rounded-lg bg-white/5 px-2.5 py-1 text-slate-400">
                  You pick at{" "}
                  <span className="tabular font-semibold text-slate-200">
                    {nextMyPick(currentPick, mySlot, teams)}
                  </span>
                </span>
              )}
            </div>
          )}

          {mode === "offline" && (
            <Badge tone="warn" title="The backend isn't running, so need and timing can't be applied.">
              <WifiOff size={11} /> Offline — value only
            </Badge>
          )}

          <div className="ml-auto flex items-center gap-2">
            {loading && <span className="text-xs text-slate-500">Updating…</span>}
            {err && <span className="max-w-[16rem] truncate text-xs text-rose-400">{err}</span>}
            <Button onClick={() => setShowLegend((v) => !v)} title="What do these columns mean?">
              <Info size={12} /> Explain
              <ChevronDown size={12} className={showLegend ? "rotate-180 transition" : "transition"} />
            </Button>
            <Button onClick={() => window.location.reload()} title="Reset board">
              <RefreshCw size={12} /> Reset
            </Button>
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

      <main className="mx-auto grid max-w-7xl grid-cols-1 gap-5 p-4 lg:grid-cols-[minmax(0,1fr)_300px]">
        <div className="min-w-0 space-y-4">
          {mode === "offline" && (
            <div className="rounded-xl border border-amber-400/25 bg-amber-500/10 px-4 py-3 text-xs leading-relaxed text-amber-200">
              The backend isn't reachable, so this is the last exported ranking: <strong>value
              only</strong>. Roster need, pick timing and the position-reliability half of
              confidence all need a live session, and aren't applied here.
            </div>
          )}

          <TopPickCard
            player={topPick}
            nextPick={nextPick}
            readOnly={readOnly}
            onOpen={() => setSelectedId(topPick.player_id)}
            onDraft={() => submitPick(topPick, true)}
            onTaken={() => submitPick(topPick, false)}
          />

          <div className="flex flex-wrap items-center gap-2">
            <div className="relative min-w-[200px] flex-1 sm:max-w-xs">
              <Search className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-600" size={14} />
              <input
                value={query}
                onChange={(e) => setQuery(e.target.value)}
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

            {mode === "live" && (
              <label className="ml-auto flex items-center gap-2.5 rounded-lg border border-white/10 bg-slate-900 px-3 py-1.5">
                <ShieldCheck size={13} className="text-slate-500" />
                <span
                  className="text-xs text-slate-400"
                  title="How much an uncertain projection is discounted. At 0 the board ranks purely on value, need and timing; higher values increasingly prefer the safer player when two are close."
                >
                  Play it safe
                </span>
                <input
                  type="range" min={0} max={0.5} step={0.05}
                  value={riskAversion}
                  onChange={(e) => setRiskAversion(Number(e.target.value))}
                  className="h-1 w-24 cursor-pointer accent-emerald-400"
                />
                <span className="tabular w-7 text-right text-xs text-slate-500">
                  {Math.round(riskAversion * 100)}%
                </span>
              </label>
            )}
          </div>

          <div className="card overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full border-collapse text-sm">
                <thead>
                  <tr className="bg-white/[0.03] text-slate-500">
                    <th className="w-10 py-2.5 pl-4 pr-2 text-right text-[11px] font-medium uppercase tracking-wider">#</th>
                    <th className="py-2.5 pr-3 text-left text-[11px] font-medium uppercase tracking-wider">Player</th>
                    <th className="py-2.5 pr-3 text-left text-[11px] font-medium uppercase tracking-wider">Pos</th>
                    <th className="hidden py-2.5 pr-3 text-right text-[11px] font-medium uppercase tracking-wider md:table-cell">VORP</th>
                    <th className="hidden py-2.5 pr-3 text-right text-[11px] font-medium uppercase tracking-wider md:table-cell">ADP</th>
                    <th className="hidden py-2.5 pr-3 text-right text-[11px] font-medium uppercase tracking-wider sm:table-cell">Available</th>
                    <th className="hidden py-2.5 pr-3 text-right text-[11px] font-medium uppercase tracking-wider sm:table-cell">Confidence</th>
                    <th className="py-2.5 pr-3 text-right text-[11px] font-medium uppercase tracking-wider">Score</th>
                    <th className="py-2.5 pl-2 pr-4" />
                  </tr>
                </thead>
                <tbody>
                  {rest.map((p, i) => (
                    <PlayerRow
                      key={p.player_id}
                      player={p}
                      rank={i + 2}
                      nextPick={nextPick}
                      readOnly={readOnly}
                      onOpen={() => setSelectedId(p.player_id)}
                      onDraft={() => submitPick(p, true)}
                      onTaken={() => submitPick(p, false)}
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

        <aside className="space-y-4 lg:sticky lg:top-[68px] lg:self-start">
          <RosterPanel rosterState={rosterState} depth={depth} picksRemaining={picksRemaining} />
          <DraftLog draftLog={draftLog} />
        </aside>
      </main>

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
