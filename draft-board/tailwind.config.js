/** @type {import('tailwindcss').Config} */

// Semantic tokens rather than raw palette classes.
//
// The app previously reached straight for `slate-900`, `emerald-400` and 119
// separate `bg-white/[0.03]` overlays. That produces the translucent, heavily
// rounded, glow-shadowed look common to generated dashboards — and it made a
// restyle a find-and-replace across two 2000-line files.
//
// The reference is FantasyPros, measured from their live ADP table rather than
// from memory: Poppins, `border-radius: 0`, 40px rows, 12px cell text, 11px
// uppercase headers at weight 500 with *normal* letter-spacing, and no cell
// borders. Their surfaces are white; ours stay dark by choice, so these are
// solid dark steps rather than white at 3% opacity — same structure, inverted.
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  theme: {
    extend: {
      fontFamily: {
        sans: ["Poppins", "ui-sans-serif", "system-ui", "-apple-system", "sans-serif"],
      },
      colors: {
        // Surface ladder. Every one is opaque: a translucent panel picks up
        // whatever is behind it, which is what made the old cards look hazy.
        surface: {
          DEFAULT: "#0a0e14",   // page
          panel: "#111722",     // cards, the table
          raised: "#18202c",    // table header, hover, active tab
          hover: "#1e2836",
          sunken: "#070a0f",    // expanded row, code blocks
        },
        line: {
          DEFAULT: "#222c3a",   // every divider and outline
          strong: "#2e3a4d",
        },
        // Every step clears WCAG AA (4.5:1) against all three surfaces, checked
        // by compositing rather than by eye - the previous ladder was inherited
        // from slate-500/600 and bottomed out at 2.4:1, which is unreadable.
        // Separation between steps is ~1.4x luminance, so the hierarchy still
        // reads.
        ink: {
          DEFAULT: "#e8edf4",   // primary text          15.3:1 on panel
          muted: "#b6c2d2",     // secondary              9.9:1
          faint: "#94a3b8",     // tertiary, headers      7.0:1
          ghost: "#7e8ea4",     // rank numbers, hints    5.4:1
        },
        // Interactive: links, active states, focus, primary buttons. Emerald
        // stops being the brand here and goes back to meaning "good value",
        // which is what it means in the data.
        accent: {
          DEFAULT: "#3b82f6",
          hover: "#60a5fa",
          soft: "#1e3a8a",
        },
        good: "#34d399",
        bad: "#f87171",
        warn: "#fbbf24",
      },
      borderRadius: {
        // FantasyPros' tables are square. Panels and rows get 2px so edges read
        // as crisp rather than hard; controls keep a little more.
        DEFAULT: "2px",
        sm: "2px",
        md: "3px",
        lg: "4px",
        xl: "6px",
        "2xl": "6px",
      },
      fontSize: {
        // The measured table scale.
        cell: ["12px", { lineHeight: "16px" }],
        head: ["11px", { lineHeight: "14px" }],
      },
    },
  },
  plugins: [],
};
