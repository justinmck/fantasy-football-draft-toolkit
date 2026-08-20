// Shared visual vocabulary for the draft board.
//
// The board is a dense, glanceable table that gets read under time pressure
// during a live draft, so colour carries meaning here rather than decoration:
// position identity, how urgent a pick is, and how much to trust a number.

// One hue per position. FantasyPros doesn't tint its POS column at all - it
// prints a positional *rank* ("RB1", "WR2") as plain text, which the board now
// does too. These survive for the places identity genuinely helps and rank
// doesn't apply: roster slots, the draft log, the pick order.
//
// Solid tints on a dark ground rather than the old `bg-x-500/15 ring-1
// ring-inset` translucent pills.
export const POSITION_STYLES = {
  QB: { chip: "bg-violet-500/20 text-violet-200", dot: "bg-violet-400" },
  RB: { chip: "bg-emerald-500/20 text-emerald-200", dot: "bg-emerald-400" },
  WR: { chip: "bg-sky-500/20 text-sky-200", dot: "bg-sky-400" },
  TE: { chip: "bg-amber-500/20 text-amber-200", dot: "bg-amber-400" },
  K:  { chip: "bg-slate-500/20 text-slate-200", dot: "bg-slate-400" },
  DST:{ chip: "bg-rose-500/20 text-rose-200", dot: "bg-rose-400" },
  FLEX:{chip: "bg-teal-500/20 text-teal-200", dot: "bg-teal-400" },
};

export const positionStyle = (pos) =>
  POSITION_STYLES[pos] || { chip: "bg-slate-500/20 text-slate-200", dot: "bg-slate-400" };

/**
 * Positional rank - "RB1", "WR2" - the way FantasyPros labels a player.
 *
 * Strictly more informative than the bare "RB" chip it replaces: third running
 * back off the board and thirtieth are the same chip and very different picks.
 * Ranked on the board's own ordering, so it tracks whatever the list is
 * currently sorted by rather than asserting a separate ranking.
 */
export function positionalRanks(players) {
  const seen = new Map();
  const out = new Map();
  for (const p of players) {
    const pos = p.position;
    if (!pos) continue;
    const n = (seen.get(pos) || 0) + 1;
    seen.set(pos, n);
    out.set(p.player_id, `${pos}${n}`);
  }
  return out;
}

// Availability is a probability the player is still on the board at your next
// turn, so low = urgent. The thresholds are deliberately coarse: during a draft
// you want "grab him / you can wait", not a number to interpret.
export function urgencyBand(availability) {
  if (availability == null || !Number.isFinite(availability)) return null;
  if (availability < 0.25) return { label: "Gone by your next pick", tone: "danger", short: "Now or never" };
  if (availability < 0.6)  return { label: "Might not last", tone: "warn", short: "Risky wait" };
  return { label: "Should still be there", tone: "calm", short: "Can wait" };
}

// Confidence blends NB04's bootstrap interval with NB05's per-position
// historical reliability (see src/scoring.py). It is NOT a probability - it's
// "how much of this number has historically held up" - so the labels avoid
// implying precision.
export function confidenceBand(confidence) {
  if (confidence == null || !Number.isFinite(confidence)) return null;
  if (confidence >= 0.55) return { label: "Well-supported projection", tone: "calm" };
  if (confidence >= 0.35) return { label: "Typical uncertainty", tone: "warn" };
  return { label: "Projection has little track record", tone: "danger" };
}

export const TONE_CLASSES = {
  danger:  "bg-rose-500/15 text-rose-200",
  warn:    "bg-amber-500/15 text-amber-200",
  calm:    "bg-emerald-500/15 text-emerald-200",
  info:    "bg-accent/15 text-accent-hover",
  neutral: "bg-surface-hover text-ink-muted",
};

export const BAR_TONE = {
  danger: "bg-bad",
  warn: "bg-warn",
  calm: "bg-good",
  neutral: "bg-ink-ghost",
};

export const fmt = (v, digits = 1) =>
  v == null || !Number.isFinite(Number(v)) ? "—" : Number(v).toFixed(digits);

export const pct = (v) =>
  v == null || !Number.isFinite(Number(v)) ? "—" : `${Math.round(Number(v) * 100)}%`;

// ADP comes through as a 999 sentinel when the player has no market data at
// all. Showing "999" reads as a real, very late pick estimate, which it isn't.
export const NO_ADP = 900;
export const fmtAdp = (v) => (v == null || Number(v) >= NO_ADP ? "—" : Math.round(Number(v)));
