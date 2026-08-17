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
 */

// Drawn at 2x and scaled down by CSS-less means: the blob carries the full
// pixel count, so it stays sharp when a phone renders it at 400pt wide.
const SCALE = 2;
const W = 1000;

const INK = {
  bg: "#020617",
  card: "#0b1220",
  line: "rgba(255,255,255,0.08)",
  text: "#f1f5f9",
  dim: "#94a3b8",
  faint: "#64748b",
  accent: "#34d399",
  accentDim: "rgba(52,211,153,0.25)",
  bar: "rgba(148,163,184,0.35)",
};

const FONT = (weight, size) =>
  `${weight} ${size}px ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif`;

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

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

/**
 * A ranked list with signed bars — the shape almost every shareable finding
 * here takes ("who drafted best", "biggest steals").
 *
 * rows: [{ label, sub, value, valueText, highlight }]
 */
export function drawRankedCard({ eyebrow, title, rows, footer, note }) {
  const PAD = 56;
  const ROW = 62;
  const headH = 212;
  const footH = footer || note ? 104 : 40;
  const H = headH + rows.length * ROW + footH;

  const canvas = document.createElement("canvas");
  canvas.width = W * SCALE;
  canvas.height = H * SCALE;
  const ctx = canvas.getContext("2d");
  ctx.scale(SCALE, SCALE);

  ctx.fillStyle = INK.bg;
  ctx.fillRect(0, 0, W, H);
  ctx.fillStyle = INK.card;
  roundRect(ctx, 16, 16, W - 32, H - 32, 28);
  ctx.fill();
  ctx.strokeStyle = INK.line;
  ctx.lineWidth = 2;
  ctx.stroke();

  // --- header ---
  ctx.fillStyle = INK.accent;
  ctx.font = FONT(700, 20);
  ctx.fillText(String(eyebrow || "").toUpperCase(), PAD, 78);

  ctx.fillStyle = INK.text;
  ctx.font = FONT(700, 42);
  ctx.fillText(fit(ctx, title, W - PAD * 2), PAD, 132);

  ctx.strokeStyle = INK.line;
  ctx.beginPath();
  ctx.moveTo(PAD, 162);
  ctx.lineTo(W - PAD, 162);
  ctx.stroke();

  // --- rows ---
  // Bars are drawn against the largest absolute value present, so the best row
  // fills its half of the track and everything else is read relative to it.
  const max = Math.max(...rows.map((r) => Math.abs(Number(r.value) || 0)), 1);
  const barX = 430;
  const barW = 330;
  const valueX = W - PAD;

  // A signed quantity has to grow from a centre line. Drawing every bar
  // rightward from a common left edge made the *worst* team's bar the second
  // longest on the card - length said "lots" while the number said "lots of
  // the wrong thing". With no negatives present there is nothing to diverge
  // from, so the track starts at the left and uses its full width.
  const diverging = rows.some((r) => (Number(r.value) || 0) < 0);
  const zeroX = diverging ? barX + barW / 2 : barX;
  const halfW = diverging ? barW / 2 : barW;

  rows.forEach((r, i) => {
    const y = headH + i * ROW;
    const mid = y - 18;
    const v = Number(r.value) || 0;
    const negative = v < 0;

    if (r.highlight) {
      ctx.fillStyle = "rgba(255,255,255,0.05)";
      roundRect(ctx, PAD - 16, y - 44, W - PAD * 2 + 32, ROW - 8, 12);
      ctx.fill();
    }

    ctx.fillStyle = INK.faint;
    ctx.font = FONT(600, 22);
    ctx.textAlign = "right";
    ctx.fillText(String(i + 1), PAD + 22, mid + 8);
    ctx.textAlign = "left";

    // "(you)" in words rather than colour alone: the highlight has to survive
    // being screenshotted, recompressed, and read by someone colour-blind.
    ctx.fillStyle = INK.text;
    ctx.font = FONT(r.highlight ? 700 : 500, 26);
    const label = fit(ctx, r.label, r.highlight ? 190 : 250);
    ctx.fillText(label, PAD + 44, mid + 2);
    if (r.highlight) {
      ctx.fillStyle = INK.accent;
      ctx.font = FONT(600, 20);
      ctx.fillText("(you)", PAD + 52 + ctx.measureText(label).width, mid + 2);
    }

    if (r.sub) {
      ctx.fillStyle = INK.faint;
      ctx.font = FONT(500, 18);
      ctx.fillText(fit(ctx, r.sub, 250), PAD + 44, mid + 26);
    }

    // Track, then the bar itself, so a short bar still reads as a proportion.
    ctx.fillStyle = "rgba(255,255,255,0.04)";
    roundRect(ctx, barX, mid - 8, barW, 16, 8);
    ctx.fill();

    const w = Math.max(3, (Math.abs(v) / max) * halfW);
    // Colour carries the sign too, so the direction is legible at thumbnail
    // size before any number is read.
    ctx.fillStyle = negative
      ? (r.highlight ? "#fb7185" : INK.bar)
      : (r.highlight ? INK.accent : INK.accentDim);
    roundRect(ctx, negative ? zeroX - w : zeroX, mid - 8, w, 16, 8);
    ctx.fill();

    if (diverging) {
      ctx.strokeStyle = "rgba(255,255,255,0.18)";
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.moveTo(zeroX, mid - 14);
      ctx.lineTo(zeroX, mid + 14);
      ctx.stroke();
    }

    ctx.fillStyle = negative ? INK.faint : INK.dim;
    ctx.font = FONT(600, 24);
    ctx.textAlign = "right";
    ctx.fillText(r.valueText ?? String(v), valueX, mid + 8);
    ctx.textAlign = "left";
  });

  // --- footer ---
  let fy = headH + rows.length * ROW + 24;
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

const toBlob = (canvas) =>
  new Promise((resolve) => canvas.toBlob(resolve, "image/png"));

/**
 * Copy image / copy text / download, for one finding.
 *
 * `draw` is a thunk so the card is only rendered when someone actually asks
 * for it — every section on this page has one of these buttons, and building
 * a dozen canvases on mount for images nobody requested would be wasteful.
 */
export default function ShareButton({ draw, text, filename = "draft-analysis" }) {
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

  const copyImage = useCallback(async () => {
    try {
      const blob = await toBlob(draw());
      await navigator.clipboard.write([new ClipboardItem({ "image/png": blob })]);
      flash("Image copied");
    } catch {
      // Firefox has no image clipboard write. Downloading is the same outcome
      // one drag later, and is better than a dead button.
      downloadImage();
      flash("Image saved");
    }
  }, [draw, flash]);

  const downloadImage = useCallback(async () => {
    const blob = await toBlob(draw());
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${filename}.png`;
    a.click();
    URL.revokeObjectURL(url);
  }, [draw, filename]);

  const copyText = useCallback(async () => {
    try {
      await navigator.clipboard.writeText(text);
      flash("Text copied");
    } catch {
      flash("Couldn't copy");
    }
  }, [text, flash]);

  const item =
    "flex w-full items-center gap-2 px-3 py-2 text-left text-xs text-slate-300 transition hover:bg-white/5";

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
            : "border-white/10 bg-white/[0.03] text-slate-400 hover:border-white/20 hover:text-slate-200"
        }`}
      >
        {done ? <Check size={12} /> : <Share2 size={12} />}
        {done || "Share"}
      </button>

      {open && (
        <div className="absolute right-0 top-full z-30 mt-1.5 w-52 overflow-hidden rounded-xl
          border border-white/10 bg-slate-900 shadow-xl shadow-black/40">
          <button onClick={copyImage} className={item}>
            <Copy size={12} className="text-slate-500" /> Copy as image
          </button>
          <button onClick={copyText} className={item}>
            <Copy size={12} className="text-slate-500" /> Copy as text
          </button>
          <button onClick={downloadImage} className={`${item} border-t border-white/5`}>
            <Download size={12} className="text-slate-500" /> Download image
          </button>
        </div>
      )}
    </div>
  );
}
