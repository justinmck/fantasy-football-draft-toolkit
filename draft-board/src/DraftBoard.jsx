import React, { useEffect, useMemo, useState } from "react";

const POSITIONS = ["ALL", "QB", "RB", "WR", "TE", "K", "DST"];
const SLOT_NEEDS = { QB: 1, RB: 2, WR: 2, TE: 1, FLEX: 1, K: 1, DST: 1 };

export default function DraftBoard() {
  const [players, setPlayers] = useState([]);
  const [myTeam, setMyTeam] = useState([]);
  const [posFilter, setPosFilter] = useState("ALL");
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [err, setErr] = useState(null);

  // ---- Load latest /public/players.json at runtime ----
  useEffect(() => {
    (async () => {
      try {
        setLoading(true);
        setErr(null);
        const res = await fetch(`/players.json?ts=${Date.now()}`, { cache: "no-store" });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const raw = await res.json();

        const mapped = (raw || []).map((r) => {
          const proj = Number(r.projected_points ?? 0);
          const vorp = Number(r.proj_vorp ?? 0);
          const avgLastYear =
            r.avg_last_year != null
              ? Number(r.avg_last_year)
              : (Number(r.points_last_year ?? 0) /
                  (Number(r.games_played_last_year ?? 0) || 1)) || 0;

          // Use your preferred weights here (manual or learned):
          const draftScore = 0.3 * proj + 0.5 * vorp + 0.2 * avgLastYear;

          return {
            id: r.player_id,
            name: r.player_name,
            pos: r.position,
            posRank: Number(r.pos_rank ?? 0),
            proj,
            adp: Number(r.adp ?? 999),
            vorp,
            avgDraftDelta: Number(r.avg_draft_delta ?? 0),
            gamesPlayedLY: Number(r.games_played_last_year ?? 0),
            pointsLY: Number(r.points_last_year ?? 0),
            avgLastYear,
            draftScore,
          };
        });

        setPlayers(mapped);
      } catch (e) {
        setErr(String(e));
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  // ---- Roster counts & open slots ----
  const posCounts = useMemo(() => {
    const counts = { QB: 0, RB: 0, WR: 0, TE: 0, K: 0, DST: 0 };
    for (const p of myTeam) if (p.pos in counts) counts[p.pos] += 1;
    return counts;
  }, [myTeam]);

  const openSlots = useMemo(() => {
    const out = { ...SLOT_NEEDS };
    for (const k of Object.keys(out)) {
      if (k === "FLEX") continue;
      out[k] = Math.max(0, (SLOT_NEEDS[k] || 0) - (posCounts[k] || 0));
    }
    const flexUsed = Math.max(
      0,
      (posCounts.RB - (SLOT_NEEDS.RB || 0)) +
        (posCounts.WR - (SLOT_NEEDS.WR || 0)) +
        (posCounts.TE - (SLOT_NEEDS.TE || 0))
    );
    out.FLEX = Math.max(0, (SLOT_NEEDS.FLEX || 0) - flexUsed);
    return out;
  }, [posCounts]);

  // ---- Filter + sort ----
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    return players
      .filter((p) => (posFilter === "ALL" ? true : p.pos === posFilter))
      .filter((p) => !q || p.name.toLowerCase().includes(q))
      .sort((a, b) => (b.draftScore ?? 0) - (a.draftScore ?? 0));
  }, [players, posFilter, query]);

  // ---- Actions ----
  const draftToMe = (id) => {
    const p = players.find((x) => x.id === id);
    if (!p) return;
    setMyTeam((t) => [...t, p]);
    setPlayers((list) => list.filter((x) => x.id !== id));
  };
  const markTaken = (id) => setPlayers((list) => list.filter((x) => x.id !== id));
  const resetBoard = () => window.location.reload(); // simple way to re-fetch latest JSON

  const IconBtn = ({ onClick, children, title }) => (
    <button
      onClick={onClick}
      title={title}
      className="h-7 whitespace-nowrap rounded-md border border-slate-300 px-2 text-xs font-medium text-slate-700 hover:bg-slate-50"
    >
      {children}
    </button>
  );

  return (
    <div className="mx-auto grid max-w-6xl grid-cols-1 gap-4 p-4 md:grid-cols-[1fr_320px]">
      <div className="space-y-3">
        <div className="flex items-center justify-between">
          <h1 className="text-lg font-semibold text-slate-800">Draft Assistant</h1>
          <div className="flex items-center gap-2">
            {loading && <span className="text-xs text-slate-500">Loading…</span>}
            {err && <span className="text-xs text-red-600">Error: {err}</span>}
            <IconBtn onClick={resetBoard} title="Reset board">
              Reset
            </IconBtn>
          </div>
        </div>

        <div className="flex flex-wrap items-center gap-2">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="Search player…"
            className="h-8 w-full max-w-xs rounded-md border border-slate-300 px-2 text-sm"
          />
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
          <div className="text-xs text-slate-500">
            Open → QB:{openSlots.QB} RB:{openSlots.RB} WR:{openSlots.WR} TE:{openSlots.TE} FLEX:{openSlots.FLEX} K:
            {openSlots.K} DST:{openSlots.DST}
          </div>
        </div>

        <div className="overflow-hidden rounded-xl border border-slate-200">
          <table className="w-full border-collapse text-sm">
            <thead className="bg-slate-50 text-slate-600">
              <tr>
                <th className="px-3 py-2 text-left font-medium">Player</th>
                <th className="px-3 py-2 text-left font-medium">Pos</th>
                <th className="px-3 py-2 text-right font-medium">Pos Rank</th>
                <th className="px-3 py-2 text-right font-medium">Proj Pts</th>
                <th className="px-3 py-2 text-right font-medium">ADP</th>
                <th className="px-3 py-2 text-right font-medium">Proj VORP</th>
                <th className="px-3 py-2 text-right font-medium">Δ ADP</th>
                <th className="px-3 py-2 text-right font-medium">Games LY</th>
                <th className="px-3 py-2 text-right font-medium">Pts LY</th>
                <th className="px-3 py-2 text-right font-medium">Draft Score</th>
                <th className="px-3 py-2 text-right font-medium">Actions</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((p) => (
                <tr key={p.id} className="border-t border-slate-200">
                  <td className="px-3 py-2">{p.name}</td>
                  <td className="px-3 py-2">{p.pos}</td>
                  <td className="px-3 py-2 text-right">{p.posRank}</td>
                  <td className="px-3 py-2 text-right">{p.proj}</td>
                  <td className="px-3 py-2 text-right">{p.adp}</td>
                  <td className="px-3 py-2 text-right">{p.vorp}</td>
                  <td className="px-3 py-2 text-right">{p.avgDraftDelta}</td>
                  <td className="px-3 py-2 text-right">{p.gamesPlayedLY}</td>
                  <td className="px-3 py-2 text-right">{p.pointsLY}</td>
                  <td className="px-3 py-2 text-right font-semibold">
                    {Number.isFinite(p.draftScore) ? p.draftScore.toFixed(1) : "-"}
                  </td>
                  <td className="px-3 py-2">
                    <div className="flex justify-end gap-1">
                      <IconBtn onClick={() => draftToMe(p.id)}>Me</IconBtn>
                      <IconBtn onClick={() => markTaken(p.id)}>Taken</IconBtn>
                    </div>
                  </td>
                </tr>
              ))}
              {!loading && filtered.length === 0 && (
                <tr>
                  <td colSpan={11} className="px-3 py-6 text-center text-sm text-slate-500">
                    No players match your filters.
                  </td>
                </tr>
              )}
              {loading && (
                <tr>
                  <td colSpan={11} className="px-3 py-6 text-center text-sm text-slate-500">
                    Loading players…
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>

      <aside className="space-y-2">
        <div className="rounded-xl border border-slate-200 p-3">
          <h2 className="mb-2 text-sm font-semibold text-slate-800">My Roster</h2>
          {myTeam.length === 0 ? (
            <div className="text-xs text-slate-500">No picks yet.</div>
          ) : (
            <ul className="divide-y divide-slate-200">
              {myTeam.map((p) => (
                <li key={p.id} className="flex items-center justify-between py-2">
                  <div>
                    <div className="truncate text-sm text-slate-800">{p.name}</div>
                    <div className="text-xs text-slate-500">
                      {p.pos} · Proj {p.proj}
                    </div>
                  </div>
                  <IconBtn
                    onClick={() => {
                      setMyTeam((team) => team.filter((x) => x.id !== p.id));
                      setPlayers((list) => [...list, p]);
                    }}
                  >
                    Undo
                  </IconBtn>
                </li>
              ))}
            </ul>
          )}
        </div>
      </aside>
    </div>
  );
}
