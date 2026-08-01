// Shared visual vocabulary for the draft board.
//
// The board is a dense, glanceable table that gets read under time pressure
// during a live draft, so colour carries meaning here rather than decoration:
// position identity, how urgent a pick is, and how much to trust a number.

// One hue per position, kept consistent everywhere they appear (chips, roster
// slots, draft log). Chosen to stay distinguishable at chip size on a dark
// background and to avoid the red/green reserved for the draft/taken actions.
export const POSITION_STYLES = {
  QB: { chip: "bg-violet-500/15 text-violet-300 ring-violet-500/30", dot: "bg-violet-400" },
  RB: { chip: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30", dot: "bg-emerald-400" },
  WR: { chip: "bg-sky-500/15 text-sky-300 ring-sky-500/30", dot: "bg-sky-400" },
  TE: { chip: "bg-amber-500/15 text-amber-300 ring-amber-500/30", dot: "bg-amber-400" },
  K: { chip: "bg-slate-500/15 text-slate-300 ring-slate-500/30", dot: "bg-slate-400" },
  DST: { chip: "bg-rose-500/15 text-rose-300 ring-rose-500/30", dot: "bg-rose-400" },
  FLEX: { chip: "bg-teal-500/15 text-teal-300 ring-teal-500/30", dot: "bg-teal-400" },
};

export const positionStyle = (pos) =>
  POSITION_STYLES[pos] || { chip: "bg-slate-500/15 text-slate-300 ring-slate-500/30", dot: "bg-slate-400" };

// Availability is a probability the player is still on the board at your next
// turn, so low = urgent. The thresholds are deliberately coarse: during a draft
// you want "grab him / you can wait", not a number to interpret.
export function urgencyBand(availability) {
  if (availability == null || !Number.isFinite(availability)) return null;
  if (availability < 0.25) return { label: "Gone by your next pick", tone: "danger", short: "Now or never" };
  if (availability < 0.6) return { label: "Might not last", tone: "warn", short: "Risky wait" };
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
  danger: "bg-rose-500/15 text-rose-300 ring-rose-500/30",
  warn: "bg-amber-500/15 text-amber-300 ring-amber-500/30",
  calm: "bg-emerald-500/15 text-emerald-300 ring-emerald-500/30",
  neutral: "bg-slate-500/15 text-slate-300 ring-slate-500/30",
};

export const BAR_TONE = {
  danger: "bg-rose-400",
  warn: "bg-amber-400",
  calm: "bg-emerald-400",
  neutral: "bg-slate-400",
};

export const fmt = (v, digits = 1) =>
  v == null || !Number.isFinite(Number(v)) ? "—" : Number(v).toFixed(digits);

export const pct = (v) =>
  v == null || !Number.isFinite(Number(v)) ? "—" : `${Math.round(Number(v) * 100)}%`;

// ADP comes through as a 999 sentinel when the player has no market data at
// all. Showing "999" reads as a real, very late pick estimate, which it isn't.
export const NO_ADP = 900;
export const fmtAdp = (v) => (v == null || Number(v) >= NO_ADP ? "—" : Math.round(Number(v)));
