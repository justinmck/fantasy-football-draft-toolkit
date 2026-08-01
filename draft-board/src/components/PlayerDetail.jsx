import React, { useEffect } from "react";
import { ShieldCheck, Sparkles, Timer, UserPlus, X, XCircle } from "lucide-react";

import { confidenceBand, fmt, fmtAdp, pct, urgencyBand } from "../theme";
import { ReasonChips } from "./explain";
import { Badge, Button, Meter, PositionChip } from "./primitives";

const Stat = ({ label, value, hint, accent }) => (
  <div>
    <div className="label">{label}</div>
    <div className={`tabular mt-0.5 text-lg font-semibold ${accent ? "text-emerald-400" : "text-slate-200"}`}>
      {value}
    </div>
    {hint && <div className="mt-0.5 text-[11px] text-slate-500">{hint}</div>}
  </div>
);

const Row = ({ label, value, hint }) => (
  <div className="flex items-baseline justify-between gap-3 border-b border-white/5 py-2 last:border-b-0">
    <span className="text-xs text-slate-400">{label}</span>
    <span className="tabular text-sm font-medium text-slate-200">
      {value}
      {hint && <span className="ml-1.5 text-[11px] font-normal text-slate-500">{hint}</span>}
    </span>
  </div>
);

/**
 * Everything known about one player, opened by clicking their row.
 *
 * The board is deliberately dense — during a live draft you scan it — so the
 * detail that doesn't fit goes here: last season's actual production, the full
 * scoring breakdown, and the projection's plausible range. The scoring
 * breakdown in particular is the point: `utility` is a product of four terms,
 * and this is where you can see each one and decide whether you agree.
 */
export default function PlayerDetail({ player, nextPick, adpYear, onClose, onDraft, onTaken, readOnly }) {
  // Escape closes, matching every other panel-style UI.
  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  if (!player) return null;

  const urgency = urgencyBand(player.availability);
  const conf = confidenceBand(player.confidence);
  const isRookie = Number(player.is_rookie) === 1;
  const games = Number(player.games_last_year) || 0;
  const played = games > 0;

  const opportunities =
    (Number(player.targets_last_year) || 0) +
    (Number(player.rush_att_last_year) || 0) +
    (Number(player.pass_att_last_year) || 0);

  return (
    <div className="fixed inset-0 z-40 flex justify-end" role="dialog" aria-modal="true">
      <div className="absolute inset-0 bg-slate-950/70" onClick={onClose} />

      <aside className="scroll-slim relative flex h-full w-full max-w-md flex-col overflow-y-auto border-l border-white/10 bg-slate-900 shadow-2xl">
        <header className="sticky top-0 z-10 border-b border-white/5 bg-slate-900/95 px-5 py-4 backdrop-blur">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <h2 className="text-xl font-semibold tracking-tight">{player.player_name}</h2>
                <PositionChip position={player.position} />
                <span className="text-sm text-slate-500">{player.pro_team}</span>
                {isRookie && (
                  <Badge
                    tone="warn"
                    title="No prior-season production on record, so the projection has no history to build on."
                  >
                    <Sparkles size={10} /> Unproven
                  </Badge>
                )}
              </div>
              <div className="mt-2">
                {/* "Unproven" is already a badge next to the name above. */}
                <ReasonChips player={player} nextPick={nextPick} max={4} exclude={["Unproven"]} />
              </div>
            </div>
            <button
              onClick={onClose}
              title="Close"
              className="shrink-0 rounded-lg border border-white/10 p-1.5 text-slate-400 transition hover:bg-white/5 hover:text-white"
            >
              <X size={16} />
            </button>
          </div>

          {!readOnly && (
            <div className="mt-4 flex gap-2">
              <Button onClick={onDraft} tone="solid" size="md" title="Draft to my team">
                <UserPlus size={14} /> Draft to my team
              </Button>
              <Button onClick={onTaken} tone="danger" size="md" title="Someone else took them">
                <XCircle size={14} /> Taken
              </Button>
            </div>
          )}
        </header>

        <div className="space-y-6 px-5 py-5">
          <section className="grid grid-cols-2 gap-x-6 gap-y-4">
            <Stat label="Score" value={fmt(player.utility, 0)} accent hint="ranking value" />
            <Stat label="VORP" value={fmt(player.vorp_z ?? player.vorp, 0)} hint="above replacement" />
            <Stat
              label="ADP"
              value={fmtAdp(player.adp)}
              hint={adpYear ? `${adpYear} market` : "market pick"}
            />
            <Stat label="Projected" value={fmt(player.projected_points, 0)} hint="points" />
          </section>

          {/* --- Timing --- */}
          {urgency && (
            <section>
              <div className="mb-2 flex items-center justify-between">
                <span className="label flex items-center gap-1.5">
                  <Timer size={12} /> Still there at pick {nextPick}
                </span>
                <span className="tabular text-sm font-semibold text-slate-200">
                  {pct(player.availability)}
                </span>
              </div>
              <Meter value={player.availability} tone={urgency.tone} />
              <p className="mt-1.5 text-xs leading-relaxed text-slate-500">
                {urgency.label}. Estimated from where the market drafts them
                {adpYear ? ` (${adpYear} ADP)` : ""} and how far away your next turn is.
              </p>
            </section>
          )}

          {/* --- Confidence --- */}
          {conf && (
            <section>
              <div className="mb-2 flex items-center justify-between">
                <span className="label flex items-center gap-1.5">
                  <ShieldCheck size={12} /> Confidence
                </span>
                <span className="tabular text-sm font-semibold text-slate-200">
                  {pct(player.confidence)}
                </span>
              </div>
              <Meter value={player.confidence} tone={conf.tone} />
              <p className="mt-1.5 text-xs leading-relaxed text-slate-500">
                {conf.label}.
                {player.reliability != null && (
                  <>
                    {" "}Projections at {player.position} have historically explained{" "}
                    <strong className="text-slate-400">{pct(player.reliability)}</strong> of the
                    variance in what those players actually delivered
                    {player.position_rmse != null && (
                      <> (typical miss {fmt(player.position_rmse, 0)} VORP)</>
                    )}
                    .
                  </>
                )}
                {isRookie && (
                  <>
                    {" "}Discounted further because there's no prior-season production behind this
                    number — historically worth about 61% as much as a veteran's.
                  </>
                )}
              </p>
              {isRookie && (
                <p className="mt-2 rounded-lg border border-amber-400/20 bg-amber-500/5 px-2.5 py-2 text-[11px] leading-relaxed text-amber-200/90">
                  Worth knowing: unproven players are less <em>predictable</em>, but not worse.
                  Across 2021–2025 they out-delivered their projections by 27 VORP on average,
                  three times the veteran figure. If you're comfortable with variance, this is
                  where the upside has been.
                </p>
              )}
              {player.ci_low != null && player.ci_high != null && (
                <div className="mt-3 rounded-lg bg-white/[0.03] px-3 py-2">
                  <div className="label mb-1">Plausible range</div>
                  <div className="tabular text-sm text-slate-300">
                    {fmt(player.ci_low, 0)} – {fmt(player.ci_high, 0)}
                    <span className="ml-2 text-xs text-slate-500">
                      VORP, 90% interval around {fmt(player.predicted_vorp, 0)}
                    </span>
                  </div>
                </div>
              )}
            </section>
          )}

          {/* --- Last season --- */}
          <section>
            <h3 className="label mb-2">Last season</h3>
            {isRookie || !played ? (
              <p className="rounded-lg bg-white/[0.03] px-3 py-3 text-xs leading-relaxed text-slate-500">
                {isRookie
                  ? "No prior-season production on record — either a rookie, or a player who didn't record stats last season. Everything above rests on the preseason projection alone."
                  : "On record but with no games played last season, so there's no recent production to check the projection against."}
              </p>
            ) : (
              <div className="rounded-lg bg-white/[0.03] px-3 py-1">
                <Row label="Fantasy points" value={fmt(player.points_last_year, 0)} />
                <Row label="Points per game" value={fmt(player.avg_last_year, 1)} />
                <Row label="Games played" value={fmt(games, 0)} hint="of 17" />
                {opportunities > 0 && (
                  <Row
                    label="Opportunities"
                    value={fmt(opportunities, 0)}
                    hint={`${fmt(opportunities / games, 1)}/game`}
                  />
                )}
                {Number(player.targets_last_year) > 0 && (
                  <Row label="Targets" value={fmt(player.targets_last_year, 0)} />
                )}
                {Number(player.rush_att_last_year) > 0 && (
                  <Row label="Rush attempts" value={fmt(player.rush_att_last_year, 0)} />
                )}
                {Number(player.pass_att_last_year) > 0 && (
                  <Row label="Pass attempts" value={fmt(player.pass_att_last_year, 0)} />
                )}
              </div>
            )}
          </section>

          {/* --- Scoring breakdown --- */}
          <section>
            <h3 className="label mb-2">How the score is built</h3>
            <div className="rounded-lg bg-white/[0.03] px-3 py-1">
              <Row label="Value (VORP, position-adjusted)" value={fmt(player.base_value, 0)} />
              <Row label="× Roster need" value={`${fmt(player.pos_weight, 2)}×`} />
              <Row label="× Pick timing" value={`${fmt(player.adp_mult, 2)}×`} />
              <Row label="× Confidence" value={`${fmt(player.risk_mult, 2)}×`} />
              <Row label="= Score" value={fmt(player.utility, 0)} />
            </div>
            <p className="mt-2 text-[11px] leading-relaxed text-slate-500">
              Small projected-points and recency terms are added on top, which is why the product
              above won't match the score exactly.
            </p>
          </section>
        </div>
      </aside>
    </div>
  );
}
