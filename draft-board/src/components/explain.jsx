import React from "react";

import { confidenceBand, fmt, fmtAdp, pct, urgencyBand } from "../theme";
import { Badge } from "./primitives";

/**
 * How far this league's own habits move a player's expected pick.
 *
 * `bias_shift` is in picks and signed the way a draft board reads: negative
 * means they go *earlier* here than the national market, so you have less time
 * than the ADP column suggests. It is a timing signal only — nothing about the
 * player's value changes — and the backend composes `bias_reason` so the app,
 * the notebooks and the Analysis tab all describe an effect the same way.
 *
 * Null-safe on purpose: the offline snapshot has no bias data at all.
 */
export function biasBand(player) {
  const shift = Number(player?.bias_shift);
  if (!Number.isFinite(shift) || !player?.bias_reason) return null;
  if (shift <= -8) return { tone: "danger", text: "League reaches here" };
  if (shift <= -2) return { tone: "warn", text: "Goes early here" };
  if (shift >= 2) return { tone: "calm", text: "Lasts longer here" };
  return null;
}

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

  // `start_weight` and `bench_weight` are the two halves of the need
  // multiplier, and they mean opposite things: one says a starting slot is
  // empty, the other says a backup here would be useful. Collapsing both into
  // "Depth only" (as this used to) described a valuable third RB and a useless
  // second kicker identically.
  const need = Number(player.pos_weight);
  const start = Number(player.start_weight);
  const benchW = Number(player.bench_weight);
  const hasSplit = Number.isFinite(start) && Number.isFinite(benchW);

  if (hasSplit ? start > 0.05 : Number.isFinite(need) && need > 1.05) {
    const big = hasSplit ? start > 0.6 : need > 1.6;
    reasons.push({
      tone: big ? "calm" : "neutral",
      text: big ? "Fills a big need" : "Fills a need",
      title: `Roster-need multiplier ${fmt(need, 2)}× — you still have open starting slots here.`,
    });
  } else if (hasSplit && benchW > 0) {
    // Bench contributions top out at 0.25 (see BENCH_WEIGHT in scoring.py);
    // the split below is roughly RB/WR vs. QB/K/DST.
    const good = benchW >= 0.12;
    reasons.push({
      tone: good ? "neutral" : "warn",
      text: good ? "Useful depth" : "Weak bench spot",
      title: good
        ? "Your starters here are set, but this position misses enough games that a backup earns its roster spot."
        : "Starters here are set, and this position almost never misses time — a backup would mostly waste a bench spot.",
    });
  } else if (Number.isFinite(need) && need <= 1.001) {
    // Starters and bench both full: there's no roster claim left to make, so
    // the board is ranking purely on who's projected to be best. Saying "Depth
    // only" here was actively misleading - it implies a reason to downgrade
    // them, when in fact nothing is being downgraded at all.
    reasons.push({
      tone: "neutral",
      text: "Best available",
      title:
        "Every starting slot and bench spot is accounted for, so roster need no longer separates players — this is ranked on projected value alone.",
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

  // Sits immediately after the urgency chip because it *explains* it: this is
  // why the availability number is what it is. Shown on its own it would read
  // as a stray fact, and below the fold it would never be seen — table rows
  // only render two chips.
  const bias = biasBand(player);
  if (bias) {
    reasons.push({
      tone: bias.tone,
      text: bias.text,
      title:
        `${player.bias_reason}. Estimated pick here: ` +
        `${fmt(player.league_pick_est, 0)} against a market ADP of ${fmtAdp(player.adp)}. ` +
        `Timing only — this does not change the player's value.`,
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
  if (low == null || high == null) return <span className="text-ink-ghost">—</span>;
  return (
    <span className="tabular text-xs text-ink-muted">
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
    term: "Reach",
    body: `How far off the market's price your league drafts this player, in picks. "11 early" means
      they've gone about eleven picks ahead of their ADP here, so waiting for the market's number
      loses them; "7 late" means they've lasted longer than the market says. It's estimated from
      this league's past drafts through the player's position and NFL team, which is why it has a
      number for players your league has never drafted. A dot marks the ones whose own record here
      backs it up — that record is shown as evidence, never folded into the estimate, because two or
      three drafts against a spread of about nineteen picks is far too little to trust on its own.
      A dash means this league has no measured history at all, which is not the same as no effect.`,
  },
  {
    term: "League timing",
    body: `Your league doesn't draft like the national market, and six seasons of its own picks say
      where. Quarterbacks go about a round earlier here than their ADP; kickers and defenses last
      longer; Philadelphia players get taken well ahead of the market. Those shifts move the
      Available estimate, and only that — the league reaching for a player doesn't make them better,
      it means they'll be gone sooner. Positions and teams with no measurable habit shift by zero,
      and every effect is pulled toward zero in proportion to how little data supports it.`,
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
