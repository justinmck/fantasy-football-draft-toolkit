import React from "react";

import { confidenceBand, fmt, pct, urgencyBand } from "../theme";
import { Badge } from "./primitives";

/**
 * The reasons a player is being recommended, as chips.
 *
 * The backend returns `utility` as a product of four terms (value x need x
 * timing x confidence - see src/scoring.py `score()`), and each term is sent
 * back as its own column precisely so this can exist. A single ranking number
 * is unarguable-with; showing which factor moved a player is what makes the
 * recommendation something you can agree or disagree with.
 */
export function reasonsFor(player, { nextPick } = {}) {
  const reasons = [];

  const need = Number(player.pos_weight);
  if (Number.isFinite(need) && need > 1.05) {
    reasons.push({
      tone: need > 1.6 ? "calm" : "neutral",
      text: need > 1.6 ? "Fills a big need" : "Fills a need",
      title: `Roster-need multiplier ${fmt(need, 2)}x — you still have open starting slots here.`,
    });
  } else if (Number.isFinite(need) && need <= 1.001) {
    reasons.push({
      tone: "neutral",
      text: "Depth only",
      title: "Your starting slots at this position are already filled, so this is bench depth.",
    });
  }

  const urgency = urgencyBand(player.availability);
  if (urgency && urgency.tone !== "calm") {
    reasons.push({
      tone: urgency.tone,
      text: urgency.short,
      title: `${pct(player.availability)} chance still on the board at pick ${nextPick ?? "your next turn"}.`,
    });
  }

  if (Number(player.is_rookie) === 1) {
    reasons.push({
      tone: "warn",
      text: "Unproven",
      title:
        "No prior-season production on record — a rookie, or a player who didn't record stats " +
        "last season. Projections for this group have been about 61% as reliable as a veteran's " +
        "at the same position, though they have out-delivered those projections on average.",
    });
  }

  const conf = confidenceBand(player.confidence);
  if (conf && conf.tone !== "warn") {
    reasons.push({
      tone: conf.tone,
      text: conf.tone === "calm" ? "Reliable projection" : "Shaky projection",
      title: conf.label,
    });
  }

  return reasons;
}

/**
 * `exclude` drops reasons already shown more prominently elsewhere - the
 * "Unproven" flag, for instance, sits next to the player's name as a badge on
 * the top card and detail panel, so repeating it as a chip is just noise.
 */
export const ReasonChips = ({ player, nextPick, max = 3, exclude = [] }) => {
  const reasons = reasonsFor(player, { nextPick })
    .filter((r) => !exclude.includes(r.text))
    .slice(0, max);
  if (!reasons.length) return null;
  return (
    <div className="flex flex-wrap items-center gap-1.5">
      {reasons.map((r) => (
        <Badge key={r.text} tone={r.tone} title={r.title}>
          {r.text}
        </Badge>
      ))}
    </div>
  );
};

/** The projection's plausible range, shown as text. */
export const RangeText = ({ player }) => {
  const { ci_low: low, ci_high: high } = player;
  if (low == null || high == null) return <span className="text-slate-600">—</span>;
  return (
    <span className="tabular text-xs text-slate-400">
      {fmt(low, 0)}–{fmt(high, 0)}
    </span>
  );
};

export const LEGEND = [
  {
    term: "Score",
    body: `The ranking number, and a product of four things rather than one: the player's
      position-adjusted VORP, how badly you still need that position, how likely they are to be
      gone before your next turn, and how much the projection can be trusted. The chips on each
      row tell you which of those is doing the work.`,
  },
  {
    term: "VORP",
    body: `Projected points above the last startable player at that position, given your league's
      roster slots — how much better this player is than the replacement you could get for free.
      It's adjusted across positions so a quarterback's steep drop-off to replacement level doesn't
      make every QB look like a first-round pick.`,
  },
  {
    term: "Available",
    body: `The chance this player is still on the board at your next turn, from their market draft
      position and how far away that turn is. Low means take them now; high means you can spend
      this pick elsewhere and come back.`,
  },
  {
    term: "Confidence",
    body: `How much of the projection has historically held up. It combines the model's own
      prediction interval with how accurate projections at that position have actually been across
      2020–2025 — kickers and defenses score near zero because their projections have explained
      essentially none of the variance in what those players actually delivered. Rookies are
      discounted further: with no prior season behind the number, their projections have been about
      61% as reliable as a veteran's.`,
  },
  {
    term: "Unproven",
    body: `A player with no prior-season production on record — a rookie, or someone who didn't
      record stats last season. They're scored more cautiously because their projections are far
      less predictable, not because they're worse: across 2021–2025 this group actually
      out-delivered their projections by 27 VORP on average, three times the veteran figure. Click
      any player for the full breakdown.`,
  },
  {
    term: "Range",
    body: `The 5th–95th percentile of the model's predicted value for this player. A wide range
      means the number in the Score column is a guess with a lot of room around it.`,
  },
];
