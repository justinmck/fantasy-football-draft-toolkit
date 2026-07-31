import React, { useCallback, useEffect, useMemo, useState } from "react";
import { RefreshCw, Search, UserPlus, XCircle, WifiOff, Trophy, Info } from "lucide-react";

const API_URL = import.meta.env.VITE_API_URL || "http://localhost:8000";
const POSITIONS = ["ALL", "QB", "RB", "WR", "TE", "K", "DST"];
const DEFAULT_ROSTER_NEED = { QB: 1, RB: 2, WR: 2, TE: 1, FLEX: 1, K: 1, DST: 1 };
const RECOMMEND_TOPN = 300; // effectively "all remaining candidates", so search/filter has the full pool

// ---- Snake-draft math ----
// Given how many picks have already happened and which draft slot is "me",
// figure out the next overall pick number and the next one that's mine.
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

const IconBtn = ({ onClick, children, title, tone = "default" }) => {
  const toneClass =
    tone === "primary"
      ? "border-emerald-300 bg-emerald-50 text-emerald-700 hover:bg-emerald-100"
      : tone === "danger"
      ? "border-rose-300 bg-rose-50 text-rose-700 hover:bg-rose-100"
      : "border-slate-300 text-slate-700 hover:bg-slate-50";
  return (
    <button
      onClick={onClick}
      title={title}
      className={`inline-flex h-7 items-center gap-1 whitespace-nowrap rounded-md border px-2 text-xs font-medium ${toneClass}`}
    >
      {children}
    </button>
  );
};

// ---- Session setup screen ----
function SetupScreen({ onStart, starting, error }) {
  const [teams, setTeams] = useState(14);
  const [mySlot, setMySlot] = useState(1);

  return (
    <div className="mx-auto flex max-w-md flex-col gap-4 p-8">
      <h1 className="text-xl font-semibold text-slate-800">Draft Assistant</h1>
      <p className="text-sm text-slate-500">
        Set up the room to get live, roster-aware recommendations during your draft.
      </p>
      <label className="text-sm text-slate-700">
        Number of teams
        <input
          type="number"
          min={2}
          max={20}
          value={teams}
          onChange={(e) => setTeams(Number(e.target.value))}
          className="mt-1 h-9 w-full rounded-md border border-slate-300 px-2 text-sm"
        />
      </label>
      <label className="text-sm text-slate-700">
        Your draft slot (1 = first overall pick)
        <input
          type="number"
          min={1}
          max={teams}
          value={mySlot}
          onChange={(e) => setMySlot(Number(e.target.value))}
          className="mt-1 h-9 w-full rounded-md border border-slate-300 px-2 text-sm"
        />
      </label>
      {error && <div className="text-xs text-red-600">{error}</div>}
      <button
        onClick={() => onStart({ teams, mySlot })}
        disabled={starting}
        className="h-9 rounded-md bg-slate-800 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50"
      >
        {starting ? "Starting…" : "Start Draft"}
      </button>
      <p className="text-xs text-slate-400">
        If the backend at <code>{API_URL}</code> isn't reachable, this falls back to a read-only
        board built from the last exported player rankings.
      </p>
    </div>
  );
}

export default function DraftBoard() {
  const [mode, setMode] = useState("setup"); // "setup" | "live" | "offline"
  const [starting, setStarting] = useState(false);
  const [setupError, setSetupError] = useState(null);

  const [sessionId, setSessionId] = useState(null);
  const [teams, setTeams] = useState(14);
  const [mySlot, setMySlot] = useState(1);

  const [pool, setPool] = useState([]);
  const [rosterState, setRosterState] = useState({});
  const [draftLog, setDraftLog] = useState([]);

  const [posFilter, setPosFilter] = useState("ALL");
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(false);
  const [err, setErr] = useState(null);
  const [showLegend, setShowLegend] = useState(false);

  const picksMade = draftLog.length;
  const currentPick = picksMade + 1;
  const nextPick = useMemo(() => nextMyPick(currentPick, mySlot, teams), [currentPick, mySlot, teams]);
  const isMyTurn = currentPick === nextPick;

  const refreshRecommendations = useCallback(
    async (sid, cp, np) => {
      setLoading(true);
      setErr(null);
      try {
        const data = await apiPost("/recommend", {
          session_id: sid,
          current_pick: cp,
          next_pick: np,
          topn: RECOMMEND_TOPN,
        });
        setPool(data.results || []);
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
          .map((r) => ({
            player_id: r.player_id,
            player_name: r.player_name,
            position: r.position,
            pro_team: r.pro_team,
            projected_points: Number(r.projected_points ?? 0),
            vorp: Number(r.proj_vorp ?? 0),
            // Sort/rank by the position-dampened proj_vorp_z, matching the
            // live-backend path (src/scoring.py score()), not raw proj_vorp -
            // otherwise offline mode would silently rank differently than live.
            adp: Number(r.adp ?? 999),
            utility: Number(r.proj_vorp_z ?? r.proj_vorp ?? 0),
          }))
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
    async ({ teams: teamsInput, mySlot: mySlotInput }) => {
      setStarting(true);
      setSetupError(null);
      setTeams(teamsInput);
      setMySlot(mySlotInput);
      try {
        const { session_id } = await apiPost("/session", {
          teams: teamsInput,
          roster_need: DEFAULT_ROSTER_NEED,
        });
        setSessionId(session_id);
        setDraftLog([]);
        setRosterState(
          Object.fromEntries(Object.entries(DEFAULT_ROSTER_NEED).map(([k, v]) => [k, { have: 0, need: v }]))
        );
        setMode("live");
        setStarting(false);
        await refreshRecommendations(session_id, 1, nextMyPick(1, mySlotInput, teamsInput));
      } catch (e) {
        await loadOfflineFallback();
      }
    },
    [refreshRecommendations, loadOfflineFallback]
  );

  const submitPick = useCallback(
    async (player, isMyPick) => {
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
        setDraftLog(data.draft_log);
        const cp = data.draft_log.length + 1;
        const np = nextMyPick(cp, mySlot, teams);
        await refreshRecommendations(sessionId, cp, np);
      } catch (e) {
        setErr(String(e));
      }
    },
    [mode, sessionId, mySlot, teams, refreshRecommendations]
  );

  const resetBoard = () => window.location.reload();

  // ---- Filter + search over whatever the current pool is (live or offline) ----
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return pool
      .filter((p) => (posFilter === "ALL" ? true : p.position === posFilter))
      .filter((p) => !q || (p.player_name || "").toLowerCase().includes(q));
  }, [pool, posFilter, query]);

  const topPick = filtered[0];
  const rest = filtered.slice(1);

  if (mode === "setup") {
    return <SetupScreen onStart={handleStart} starting={starting} error={setupError} />;
  }

  return (
    <div className="mx-auto grid max-w-6xl grid-cols-1 gap-4 p-4 md:grid-cols-[1fr_320px]">
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <h1 className="text-lg font-semibold text-slate-800">Draft Assistant</h1>
          <div className="flex items-center gap-2">
            {mode === "offline" && (
              <span className="inline-flex items-center gap-1 rounded-md bg-amber-100 px-2 py-1 text-xs font-medium text-amber-800">
                <WifiOff size={12} /> Offline mode — showing last exported rankings
              </span>
            )}
            {loading && <span className="text-xs text-slate-500">Updating…</span>}
            {err && <span className="text-xs text-red-600">Error: {err}</span>}
            <IconBtn onClick={() => setShowLegend((v) => !v)} title="What do VORP / Score / ADP mean?">
              <Info size={12} /> Explain
            </IconBtn>
            <IconBtn onClick={resetBoard} title="Reset board">
              <RefreshCw size={12} /> Reset
            </IconBtn>
          </div>
        </div>

        {showLegend && (
          <div className="rounded-xl border border-slate-200 bg-slate-50 p-3 text-xs leading-relaxed text-slate-600">
            <dl className="space-y-2">
              <div>
                <dt className="font-semibold text-slate-800">VORP (Value Over Replacement Player)</dt>
                <dd>
                  Projected points above the last startable player at that position, given your
                  league's roster slots — i.e. how much better this player is than the replacement
                  you could get for free.
                </dd>
              </div>
              <div>
                <dt className="font-semibold text-slate-800">Score</dt>
                <dd>
                  The ranking number. It starts from VORP, but with a twist: some positions (QB
                  especially) have a much steeper points drop-off from the best player to the
                  replacement level than others, which inflates their raw VORP even though only
                  one starts per team. Score dampens that so positions aren't ranked purely by
                  scale — this is why a player with huge VORP can still show a lower Score than
                  someone at a shallower position. Score also factors in your remaining roster
                  needs and ADP pressure (how likely the player is to be gone before your next
                  pick).
                </dd>
              </div>
              <div>
                <dt className="font-semibold text-slate-800">ADP</dt>
                <dd>Average draft position — where the market typically takes this player.</dd>
              </div>
            </dl>
          </div>
        )}

        {mode === "live" && (
          <div className="flex flex-wrap items-center gap-3 text-xs text-slate-500">
            <span>
              Pick <strong className="text-slate-700">{currentPick}</strong> on the clock
            </span>
            <span>
              Your next pick: <strong className="text-slate-700">{nextPick}</strong>
            </span>
            {isMyTurn && (
              <span className="rounded-md bg-emerald-100 px-2 py-0.5 font-medium text-emerald-800">
                It's your turn!
              </span>
            )}
          </div>
        )}

        {topPick && (
          <div className="flex items-center justify-between rounded-xl border border-emerald-200 bg-emerald-50 p-4">
            <div className="flex items-center gap-3">
              <Trophy className="text-emerald-600" size={28} />
              <div>
                <div className="text-xs font-medium uppercase tracking-wide text-emerald-700">
                  Top Recommendation
                </div>
                <div className="text-lg font-semibold text-slate-800">
                  {topPick.player_name}{" "}
                  <span className="text-sm font-normal text-slate-500">
                    {topPick.position} · {topPick.pro_team}
                  </span>
                </div>
                <div className="text-xs text-slate-500">
                  Proj {topPick.projected_points} · VORP {Number(topPick.vorp).toFixed(1)} · ADP{" "}
                  {topPick.adp}
                </div>
              </div>
            </div>
            <div className="flex gap-2">
              <IconBtn onClick={() => submitPick(topPick, true)} tone="primary" title="Draft to my team">
                <UserPlus size={12} /> Me
              </IconBtn>
              <IconBtn onClick={() => submitPick(topPick, false)} tone="danger" title="Mark as taken">
                <XCircle size={12} /> Taken
              </IconBtn>
            </div>
          </div>
        )}

        <div className="flex flex-wrap items-center gap-2">
          <div className="relative w-full max-w-xs">
            <Search className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-slate-400" size={14} />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search player…"
              className="h-8 w-full rounded-md border border-slate-300 pl-7 pr-2 text-sm"
            />
          </div>
          <select
            value={posFilter}
            onChange={(e) => setPosFilter(e.target.value)}
            className="h-8 rounded-md border border-slate-300 bg-white px-2 text-sm"
          >
            {POSITIONS.map((p) => (
              <option key={p} value={p}>
                {p}
              </option>
            ))}
          </select>
        </div>

        <div className="overflow-hidden rounded-xl border border-slate-200">
          <table className="w-full border-collapse text-sm">
            <thead className="bg-slate-50 text-slate-600">
              <tr>
                <th className="px-3 py-2 text-left font-medium">Player</th>
                <th className="px-3 py-2 text-left font-medium">Pos</th>
                <th className="px-3 py-2 text-right font-medium">Proj Pts</th>
                <th className="px-3 py-2 text-right font-medium">ADP</th>
                <th className="px-3 py-2 text-right font-medium">VORP</th>
                <th className="px-3 py-2 text-right font-medium">Score</th>
                <th className="px-3 py-2 text-right font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {rest.map((p) => (
                <tr key={p.player_id} className="border-t border-slate-200">
                  <td className="px-3 py-2">{p.player_name}</td>
                  <td className="px-3 py-2">{p.position}</td>
                  <td className="px-3 py-2 text-right">{p.projected_points}</td>
                  <td className="px-3 py-2 text-right">{p.adp}</td>
                  <td className="px-3 py-2 text-right">{Number(p.vorp).toFixed(1)}</td>
                  <td className="px-3 py-2 text-right font-semibold">
                    {Number.isFinite(p.utility) ? Number(p.utility).toFixed(1) : "-"}
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex justify-end gap-1">
                      <IconBtn onClick={() => submitPick(p, true)} title="Draft to my team">
                        <UserPlus size={12} /> Me
                      </IconBtn>
                      <IconBtn onClick={() => submitPick(p, false)} title="Mark as taken">
                        <XCircle size={12} /> Taken
                      </IconBtn>
                    </div>
                  </td>
                </tr>
              ))}
              {!loading && filtered.length === 0 && (
                <tr>
                  <td colSpan={7} className="px-3 py-6 text-center text-sm text-slate-500">
                    No players match your filters.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <aside className="space-y-3">
        <div className="rounded-xl border border-slate-200 p-3">
          <h2 className="mb-2 text-sm font-semibold text-slate-800">My Roster</h2>
          {Object.keys(rosterState).length === 0 ? (
            <div className="text-xs text-slate-500">No roster data.</div>
          ) : (
            <ul className="grid grid-cols-2 gap-1 text-xs text-slate-600">
              {Object.entries(rosterState).map(([pos, v]) => (
                <li key={pos} className="flex justify-between rounded bg-slate-50 px-2 py-1">
                  <span>{pos}</span>
                  <span className="font-medium text-slate-800">
                    {v.have}/{v.need}
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="rounded-xl border border-slate-200 p-3">
          <h2 className="mb-2 text-sm font-semibold text-slate-800">Draft Log</h2>
          {draftLog.length === 0 ? (
            <div className="text-xs text-slate-500">No picks yet.</div>
          ) : (
            <ul className="max-h-96 divide-y divide-slate-200 overflow-y-auto">
              {[...draftLog].reverse().map((p) => (
                <li key={p.pick_number} className="flex items-center justify-between py-1.5 text-xs">
                  <span className="text-slate-400">#{p.pick_number}</span>
                  <span className="flex-1 truncate px-2 text-slate-800">{p.player_name}</span>
                  <span className="text-slate-500">{p.position}</span>
                  {p.is_my_pick && (
                    <span className="ml-2 rounded bg-emerald-100 px-1.5 py-0.5 font-medium text-emerald-700">
                      You
                    </span>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      </aside>
    </div>
  );
}
