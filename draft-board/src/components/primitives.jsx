import React from "react";

import { BAR_TONE, TONE_CLASSES, positionStyle } from "../theme";

export const PositionChip = ({ position }) => (
  <span className={`chip ${positionStyle(position).chip}`}>{position}</span>
);

export const Badge = ({ tone = "neutral", children, title }) => (
  <span className={`chip ${TONE_CLASSES[tone] || TONE_CLASSES.neutral}`} title={title}>
    {children}
  </span>
);

export const Button = ({ onClick, children, title, tone = "default", size = "sm", disabled }) => {
  // Solid surfaces with real borders, not white at 5% opacity. `primary` is the
  // interactive blue; `solid` is the one filled call to action per screen.
  const tones = {
    default: "border-line bg-surface-raised text-ink-muted hover:bg-surface-hover hover:text-ink",
    primary: "border-accent/40 bg-accent/15 text-accent-hover hover:bg-accent/25",
    danger: "border-bad/30 bg-bad/10 text-bad hover:bg-bad/20",
    solid: "border-transparent bg-accent-fill text-white hover:bg-accent-fill-hover font-semibold",
  };
  const sizes = { sm: "h-7 px-2 text-xs", md: "h-9 px-3 text-sm", lg: "h-11 px-4 text-sm" };
  return (
    <button
      onClick={onClick}
      title={title}
      disabled={disabled}
      className={`inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded border
        transition-colors disabled:cursor-not-allowed disabled:opacity-40
        ${tones[tone]} ${sizes[size]}`}
    >
      {children}
    </button>
  );
};

/**
 * A short horizontal meter. Used for the two quantities that are much easier to
 * compare visually than numerically while a draft is running: how likely a
 * player is to still be there next turn, and how much the projection can be
 * trusted.
 */
export const Meter = ({ value, tone = "neutral", className = "" }) => {
  const width = Math.max(0, Math.min(1, Number(value) || 0)) * 100;
  return (
    <div className={`h-1 w-full overflow-hidden rounded-sm bg-surface-hover ${className}`}>
      <div
        className={`h-full rounded-sm transition-[width] duration-300 ${BAR_TONE[tone] || BAR_TONE.neutral}`}
        style={{ width: `${width}%` }}
      />
    </div>
  );
};

/**
 * Roster slots drawn as pips rather than "1/2" text. At a glance during a draft
 * you want to see *shape* - which rows are still empty - not read fractions.
 */
export const SlotPips = ({ have, need, position }) => {
  const dot = positionStyle(position).dot;
  return (
    <span className="flex items-center gap-1">
      {Array.from({ length: need }).map((_, i) => (
        <span
          key={i}
          className={`h-2 w-2 rounded-sm ${i < have ? dot : "bg-surface-hover"}`}
        />
      ))}
    </span>
  );
};
