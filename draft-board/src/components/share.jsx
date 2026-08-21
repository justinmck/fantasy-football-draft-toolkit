import React, { useCallback, useEffect, useRef, useState } from "react";
import { Check, Copy, Download, Share2 } from "lucide-react";

/**
 * Turning a section of the analysis into something you can paste in a group chat.
 *
 * The card is **drawn on a canvas from the data**, not screenshotted from the
 * DOM. Two reasons that's worth the extra code:
 *
 *   1. A screenshot of this page pastes as a crop of a dark dashboard, at
 *      whatever size the reader's window happened to be, with clipped edges and
 *      half a scrollbar. A drawn card is composed for the medium it's going to:
 *      fixed size, big type, one message.
 *   2. It needs no dependency. Every DOM-to-image library is a few hundred
 *      kilobytes and a pile of caveats about web fonts and CSS it can't parse.
 *
 * Text is offered alongside the image, because plenty of the value here is a
 * ranked list and a ranked list reads perfectly well as text - and text is what
 * survives being quoted, replied to and screenshotted back at you.
 *
 * **The card composes blocks.** It began as one fixed layout - a ranked list of
 * entities with one signed number each - which is the shape three sections take
 * and fourteen don't. A replacement-level table's finding is the gap between
 * two columns; the rookie section is four figures and no list at all. Flattening
 * those into a ranking wouldn't have shared them, it would have shared
 * something else. So `stats`, `rows` and `table` are separate blocks, each
 * optional, and a section supplies whichever ones it actually has.
 */

// Drawn at 2x and scaled down by CSS-less means: the blob carries the full
// pixel count, so it stays sharp when a phone renders it at 400pt wide.
const SCALE = 2;
const W = 1000;

// Past this the card becomes a portrait strip that chat clients downscale to
// illegibility - a fourteen-team league is fine, a thirty-two-team NFL table is
// not. The overflow is stated rather than silently dropped.
const MAX_ROWS = 16;

// A two-row card at natural height is 1000x356, which pastes as a letterbox.
// Short cards get padded out to something closer to square instead.
const MIN_H = 520;

// A canvas can't read Tailwind, so this mirrors the tokens in
// tailwind.config.js by hand. Keep the two in step - this file is easy to
// forget, and a stale palette here ships in the image people actually send.
const INK = {
  bg: "#0a0e14",          // surface
  card: "#111722",        // surface.panel
  raised: "#18202c",      // surface.raised - stat tiles, table header band
  line: "#222c3a",        // line
  text: "#e8edf4",        // ink
  dim: "#b6c2d2",         // ink.muted
  faint: "#94a3b8",       // ink.faint
  ghost: "#7e8ea4",       // ink.ghost
  accent: "#34d399",      // good - a positive value, not the brand
  accentDim: "rgba(52,211,153,0.28)",
  bar: "rgba(148,163,184,0.35)",
  rule: "rgba(255,255,255,0.16)",
  highlight: "rgba(255,255,255,0.05)",
  track: "rgba(255,255,255,0.05)",
  negative: "#f87171",    // bad
  warn: "#fbbf24",
};

const TONES = { good: INK.accent, bad: INK.negative, warn: INK.warn, dim: INK.faint };

// Poppins, matching the app. Falls back cleanly if the face hasn't loaded -
// the card is drawn on demand, well after first paint, so in practice it has.
const FONT = (weight, size) =>
  `${weight} ${size}px Poppins, ui-sans-serif, system-ui, -apple-system, sans-serif`;

const PAD = 56;
const HEAD_H = 212;

/** Trim a string to fit `max` px, ending in an ellipsis rather than mid-word. */
function fit(ctx, text, max) {
  const s = String(text ?? "");
  if (ctx.measureText(s).width <= max) return s;
  let out = s;
  while (out.length > 1 && ctx.measureText(`${out}…`).width > max) {
    out = out.slice(0, -1);
  }
  return `${out}…`;
}

/** Break into at most `maxLines` lines of `max` px, ellipsizing the last. */
function wrap(ctx, text, max, maxLines) {
  const words = String(text ?? "").split(/\s+/).filter(Boolean);
  const lines = [];
  let i = 0;
  while (i < words.length && lines.length < maxLines) {
    let line = words[i++];
    while (i < words.length && ctx.measureText(`${line} ${words[i]}`).width <= max) {
      line += ` ${words[i++]}`;
    }
    // On the last line, absorb everything still unplaced so `fit` can ellipsize
    // it - otherwise the tail is silently dropped and the label reads as though
    // it ended there.
    const last = lines.length === maxLines - 1;
    if (last && i < words.length) line += ` ${words.slice(i).join(" ")}`;
    lines.push(fit(ctx, line, max));
    if (last) break;
  }
  return lines;
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

/** How many rows a block will actually draw, and how many it had to leave out. */
const capped = (list) => {
  const all = list || [];
  return { shown: all.slice(0, MAX_ROWS), hidden: Math.max(0, all.length - MAX_ROWS) };
};

const STAT_H = 108;
const ROW_H = 62;
const THEAD_H = 42;
const TROW_H = 46;
const MORE_H = 34;

/**
 * A shareable card, composed of whichever blocks the section has.
 *
 *   stats: [{ value, label, tone }]                    up to 4 tiles
 *   rows:  [{ label, sub, value, valueText, highlight }]  ranked list with bars
 *   table: { head, align, rows: [[cell, …]], highlight: [i] }
 *
 * `rows` and `table` are alternatives; `stats` can precede either or stand
 * alone. Returns null when there is nothing to draw, so the button can hide
 * itself rather than offer a blank image.
 */
export function drawCard({ eyebrow, title, stats, rows, table, footer, note }) {
  const tiles = (stats || []).slice(0, 4);
  const rank = capped(rows);
  const grid = capped(table?.rows);
  if (!tiles.length && !rank.shown.length && !grid.shown.length) return null;

  const statsH = tiles.length ? STAT_H + 28 : 0;
  const rowsH = rank.shown.length
    ? rank.shown.length * ROW_H + (rank.hidden ? MORE_H : 0)
    : 0;
  const tableH = grid.shown.length
    ? THEAD_H + grid.shown.length * TROW_H + (grid.hidden ? MORE_H : 0)
    : 0;
  const footH = footer || note ? 104 : 48;
  const H = Math.max(MIN_H, HEAD_H + statsH + rowsH + tableH + footH);

  const canvas = document.createElement("canvas");
  canvas.width = W * SCALE;
  canvas.height = H * SCALE;
  const ctx = canvas.getContext("2d");
  ctx.scale(SCALE, SCALE);

  ctx.fillStyle = INK.bg;
  ctx.fillRect(0, 0, W, H);
  ctx.fillStyle = INK.card;
  roundRect(ctx, 16, 16, W - 32, H - 32, 4);
  ctx.fill();
  ctx.strokeStyle = INK.line;
  ctx.lineWidth = 2;
  ctx.stroke();

  // --- header ---
  ctx.fillStyle = INK.accent;
  ctx.font = FONT(700, 20);
  ctx.fillText(fit(ctx, String(eyebrow || "").toUpperCase(), W - PAD * 2), PAD, 78);

  ctx.fillStyle = INK.text;
  ctx.font = FONT(700, 42);
  ctx.fillText(fit(ctx, title, W - PAD * 2), PAD, 132);

  ctx.strokeStyle = INK.line;
  ctx.beginPath();
  ctx.moveTo(PAD, 162);
  ctx.lineTo(W - PAD, 162);
  ctx.stroke();

  let y = HEAD_H;
  if (tiles.length) y = drawStats(ctx, tiles, y);
  if (rank.shown.length) y = drawRows(ctx, rank, y);
  else if (grid.shown.length) y = drawTable(ctx, table, grid, y);

  // --- footer ---
  let fy = y + 24;
  if (footer) {
    ctx.fillStyle = INK.dim;
    ctx.font = FONT(500, 22);
    ctx.fillText(fit(ctx, footer, W - PAD * 2), PAD, fy);
    fy += 32;
  }
  if (note) {
    ctx.fillStyle = INK.faint;
    ctx.font = FONT(400, 18);
    ctx.fillText(fit(ctx, note, W - PAD * 2), PAD, fy);
  }

  // Wordmark, bottom right - it's the only thing identifying where the numbers
  // came from once the image is three chats deep.
  ctx.fillStyle = INK.faint;
  ctx.font = FONT(500, 18);
  ctx.textAlign = "right";
  ctx.fillText("Justin's Draft Assistant", W - PAD, H - 40);
  ctx.textAlign = "left";

  return canvas;
}

/** The canvas twin of the `Figure` tiles on the page. */
function drawStats(ctx, tiles, top) {
  const gap = 14;
  const w = (W - PAD * 2 - gap * (tiles.length - 1)) / tiles.length;

  tiles.forEach((t, i) => {
    const x = PAD + i * (w + gap);
    ctx.fillStyle = INK.raised;
    roundRect(ctx, x, top, w, STAT_H, 4);
    ctx.fill();
    ctx.strokeStyle = INK.line;
    ctx.lineWidth = 1.5;
    ctx.stroke();

    ctx.fillStyle = TONES[t.tone] || INK.text;
    ctx.font = FONT(700, 34);
    ctx.fillText(fit(ctx, t.value, w - 28), x + 14, top + 46);

    ctx.fillStyle = INK.faint;
    ctx.font = FONT(400, 16);
    wrap(ctx, t.label, w - 28, 2).forEach((line, n) => {
      ctx.fillText(line, x + 14, top + 72 + n * 20);
    });
  });

  return top + STAT_H + 28;
}

/** A ranked list with signed bars - the shape most findings here take. */
function drawRows(ctx, { shown, hidden }, top) {
  // Bars are drawn against the largest absolute value present, so the best row
  // fills its half of the track and everything else is read relative to it.
  const max = Math.max(...shown.map((r) => Math.abs(Number(r.value) || 0)), 1);
  const barX = 430;
  const barW = 330;
  const valueX = W - PAD;

  // A signed quantity has to grow from a center line. Drawing every bar
  // rightward from a common left edge made the *worst* team's bar the second
  // longest on the card - length said "lots" while the number said "lots of
  // the wrong thing". With no negatives present there is nothing to diverge
  // from, so the track starts at the left and uses its full width.
  const diverging = shown.some((r) => (Number(r.value) || 0) < 0);
  const zeroX = diverging ? barX + barW / 2 : barX;
  const halfW = diverging ? barW / 2 : barW;

  shown.forEach((r, i) => {
    const y = top + i * ROW_H;
    const mid = y + ROW_H - 18;
    const v = Number(r.value) || 0;
    const negative = v < 0;

    if (r.highlight) {
      ctx.fillStyle = INK.highlight;
      roundRect(ctx, PAD - 16, y + 6, W - PAD * 2 + 32, ROW_H - 8, 3);
      ctx.fill();
    }

    ctx.fillStyle = INK.ghost;
    ctx.font = FONT(600, 22);
    ctx.textAlign = "right";
    ctx.fillText(String(i + 1), PAD + 22, mid - 10);
    ctx.textAlign = "left";

    // "(you)" in words rather than color alone: the highlight has to survive
    // being screenshotted, recompressed, and read by someone color-blind.
    ctx.fillStyle = INK.text;
    ctx.font = FONT(r.highlight ? 700 : 500, 26);
    const label = fit(ctx, r.label, r.highlight ? 190 : 250);
    ctx.fillText(label, PAD + 44, mid - 16);
    if (r.highlight) {
      ctx.fillStyle = INK.accent;
      ctx.font = FONT(600, 20);
      ctx.fillText("(you)", PAD + 52 + ctx.measureText(label).width, mid - 16);
    }

    if (r.sub) {
      ctx.fillStyle = INK.faint;
      ctx.font = FONT(500, 18);
      ctx.fillText(fit(ctx, r.sub, 250), PAD + 44, mid + 8);
    }

    // Track, then the bar itself, so a short bar still reads as a proportion.
    ctx.fillStyle = INK.track;
    roundRect(ctx, barX, mid - 26, barW, 16, 3);
    ctx.fill();

    const w = Math.max(3, (Math.abs(v) / max) * halfW);
    // Color carries the sign too, so the direction is legible at thumbnail
    // size before any number is read.
    ctx.fillStyle = negative
      ? (r.highlight ? INK.negative : INK.bar)
      : (r.highlight ? INK.accent : INK.accentDim);
    roundRect(ctx, negative ? zeroX - w : zeroX, mid - 26, w, 16, 3);
    ctx.fill();

    if (diverging) {
      ctx.strokeStyle = INK.rule;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(zeroX, mid - 32);
      ctx.lineTo(zeroX, mid - 4);
      ctx.stroke();
    }

    ctx.fillStyle = negative ? INK.faint : INK.dim;
    ctx.font = FONT(600, 24);
    ctx.textAlign = "right";
    ctx.fillText(r.valueText ?? String(v), valueX, mid - 10);
    ctx.textAlign = "left";
  });

  let end = top + shown.length * ROW_H;
  if (hidden) end = drawMore(ctx, hidden, end);
  return end;
}

/**
 * A plain grid, for findings that live in the relationship between columns.
 *
 * Replacement level is the clearest case: the point is the distance between
 * the baseline and the best available, which a single-value ranking cannot
 * express at all.
 */
function drawTable(ctx, spec, { shown, hidden }, top) {
  const head = spec.head || [];
  const align = spec.align || [];
  const marked = new Set(spec.highlight || []);
  const avail = W - PAD * 2;
  // The first column carries names and gets a third; the rest are numbers and
  // split what's left evenly.
  const firstW = head.length > 1 ? avail * 0.34 : avail;
  const restW = head.length > 1 ? (avail - firstW) / (head.length - 1) : 0;
  const colX = head.map((_, i) => PAD + (i === 0 ? 0 : firstW + (i - 1) * restW));
  const colW = head.map((_, i) => (i === 0 ? firstW : restW));
  const right = (i) => align[i] === "right" || (i > 0 && align[i] !== "left");

  ctx.fillStyle = INK.raised;
  roundRect(ctx, PAD - 12, top, avail + 24, THEAD_H, 3);
  ctx.fill();

  ctx.fillStyle = INK.faint;
  ctx.font = FONT(500, 16);
  head.forEach((h, i) => {
    const label = String(h ?? "").toUpperCase();
    if (right(i)) {
      ctx.textAlign = "right";
      ctx.fillText(fit(ctx, label, colW[i] - 16), colX[i] + colW[i], top + 27);
      ctx.textAlign = "left";
    } else {
      ctx.fillText(fit(ctx, label, colW[i] - 16), colX[i], top + 27);
    }
  });

  shown.forEach((row, r) => {
    const y = top + THEAD_H + r * TROW_H;
    if (marked.has(r)) {
      ctx.fillStyle = INK.highlight;
      roundRect(ctx, PAD - 12, y + 2, avail + 24, TROW_H - 4, 3);
      ctx.fill();
    }
    row.slice(0, head.length).forEach((cell, i) => {
      ctx.fillStyle = i === 0 ? INK.text : INK.dim;
      ctx.font = FONT(i === 0 || marked.has(r) ? 600 : 500, 22);
      const s = String(cell ?? "—");
      if (right(i)) {
        ctx.textAlign = "right";
        ctx.fillText(fit(ctx, s, colW[i] - 16), colX[i] + colW[i], y + 31);
        ctx.textAlign = "left";
      } else {
        ctx.fillText(fit(ctx, s, colW[i] - 16), colX[i], y + 31);
      }
    });
    ctx.strokeStyle = INK.line;
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(PAD - 12, y + TROW_H);
    ctx.lineTo(W - PAD + 12, y + TROW_H);
    ctx.stroke();
  });

  let end = top + THEAD_H + shown.length * TROW_H;
  if (hidden) end = drawMore(ctx, hidden, end);
  return end;
}

/** Say what was left off, rather than letting the card imply it's the whole list. */
function drawMore(ctx, hidden, top) {
  ctx.fillStyle = INK.ghost;
  ctx.font = FONT(500, 18);
  ctx.fillText(`+${hidden} more`, PAD + 44, top + 22);
  return top + MORE_H;
}

/**
 * The same card as plain text.
 *
 * Generated from the identical object the image is drawn from, so the two
 * cannot drift - which they would, written twice, across seventeen sections.
 */
export function cardText({ eyebrow, title, stats, rows, table, footer, note }) {
  const out = [];
  out.push([eyebrow, title].filter(Boolean).join(" — "));
  out.push("");

  for (const t of (stats || []).slice(0, 4)) out.push(`${t.label}: ${t.value}`);
  if (stats?.length) out.push("");

  const rank = capped(rows);
  rank.shown.forEach((r, i) => {
    const sub = r.sub ? ` (${r.sub})` : "";
    out.push(`${String(i + 1).padStart(2)}. ${r.label}${r.highlight ? " (me)" : ""} — ` +
             `${r.valueText ?? r.value}${sub}`);
  });
  if (rank.hidden) out.push(`    +${rank.hidden} more`);
  if (rank.shown.length) out.push("");

  const grid = capped(table?.rows);
  if (grid.shown.length) {
    const head = (table.head || []).map((h) => String(h ?? ""));
    const body = grid.shown.map((r) => r.slice(0, head.length).map((c) => String(c ?? "—")));
    // Pad to the widest cell per column so the columns line up in a monospaced
    // chat window, which is where this text is going to land.
    const width = head.map((h, i) =>
      Math.max(h.length, ...body.map((r) => (r[i] || "").length)));
    const line = (cells) =>
      cells.map((c, i) => (i === 0 ? c.padEnd(width[i]) : c.padStart(width[i]))).join("  ");
    out.push(line(head));
    body.forEach((r) => out.push(line(r)));
    if (grid.hidden) out.push(`+${grid.hidden} more`);
    out.push("");
  }

  if (footer) out.push(footer);
  if (note) out.push(note);
  out.push("");
  out.push("via Justin's Draft Assistant");
  return out.join("\n").replace(/\n{3,}/g, "\n\n").trim();
}

/**
 * One call site's worth of sharing: the card object in, the button's props out.
 *
 * `text` is derived from the same object unless a section hands over something
 * better written by hand.
 */
export const share = (card, filename, text) => ({
  draw: () => drawCard(card),
  text: text ?? cardText(card),
  filename,
  empty: !(card.stats?.length || card.rows?.length || card.table?.rows?.length),
});

/** Back-compat for the ranked-only shape. */
export const drawRankedCard = (card) => drawCard(card);

const toBlob = (canvas) =>
  new Promise((resolve) => canvas.toBlob(resolve, "image/png"));

/**
 * Copy image / copy text / download, for one finding.
 *
 * `draw` is a thunk so the card is only rendered when someone actually asks
 * for it — every section on this page has one of these buttons, and building
 * a dozen canvases on mount for images nobody requested would be wasteful.
 */
export default function ShareButton({ draw, text, filename = "draft-analysis", empty }) {
  const [open, setOpen] = useState(false);
  const [done, setDone] = useState(null);
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

  // The confirmation is the whole feedback loop for a clipboard action - there
  // is nothing else on screen to tell you it worked.
  const flash = useCallback((msg) => {
    setDone(msg);
    setOpen(false);
    setTimeout(() => setDone(null), 2000);
  }, []);

  const downloadImage = useCallback(async () => {
    const canvas = draw();
    if (!canvas) return;
    const blob = await toBlob(canvas);
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${filename}.png`;
    a.click();
    URL.revokeObjectURL(url);
  }, [draw, filename]);

  const copyImage = useCallback(async () => {
    try {
      const canvas = draw();
      if (!canvas) return;
      const blob = await toBlob(canvas);
      await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
      flash("Image copied");
    } catch {
      // Firefox has no image clipboard write. Downloading is the same outcome
      // one drag later, and is better than a dead button.
      downloadImage();
      flash("Image saved");
    }
  }, [draw, flash, downloadImage]);

  const copyText = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(text);
      flash("Text copied");
    } catch {
      flash("Couldn't copy");
    }
  }, [text, flash]);

  // A section whose data didn't arrive has nothing to send. Better no button
  // than one that hands over an empty frame with a title on it.
  if (empty) return null;

  const item =
    "flex w-full items-center gap-2 px-3 py-2 text-left text-xs text-ink-muted transition hover:bg-surface-raised";

  return (
    <div className="relative shrink-0" ref={box}>
      <button
        onClick={() => setOpen((v) => !v)}
        title="Share this"
        aria-label="Share this"
        aria-expanded={open}
        className={`flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs transition ${
          done
            ? "border-emerald-400/40 bg-emerald-500/10 text-emerald-300"
            : "border-line bg-surface-raised text-ink-muted hover:border-line-strong hover:text-ink"
        }`}
      >
        {done ? <Check size={12} /> : <Share2 size={12} />}
        {done || "Share"}
      </button>

      {open && (
        <div className="absolute right-0 top-full z-30 mt-1.5 w-52 overflow-hidden rounded-xl
 border border-line bg-surface-panel shadow-lg shadow-black/60">
          <button onClick={copyImage} className={item}>
            <Copy size={12} className="text-ink-faint" /> Copy as image
          </button>
          <button onClick={copyText} className={item}>
            <Copy size={12} className="text-ink-faint" /> Copy as text
          </button>
          <button onClick={downloadImage} className={`${item} border-t border-line/60`}>
            <Download size={12} className="text-ink-faint" /> Download image
          </button>
        </div>
      )}
    </div>
  );
}
