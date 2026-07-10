# README Diagrams — Agent Reference

> **Audience:** Any agent (or human) editing the architecture figures in the top-level README.
> **Status:** Complete
> **Last Updated:** 2026-07-10

This directory holds the four architecture diagrams embedded in [`README.md`](../../README.md)
and the script that generates them. Everything below is what was learned building them — read
it before touching a diagram so you don't relearn the gotchas the hard way.

## What's here

| File | What it is |
|------|-----------|
| `generate_diagrams.py` | **Source of truth.** Emits all four SVGs. Pure stdlib, no deps. |
| `architecture.svg` | System map: sources → local core → orchestrator → surfaces + autonomous. |
| `query-pipeline.svg` | One query's path: input surfaces, intra-query tool loop, model handoff, output. |
| `sync-cycle.svg` | The nightly 7-phase sync as a circular cycle. |
| `services.svg` | Service resilience tiers (critical / graceful / external) by failure impact. |
| `*.png` | Screenshots for the README's CRM section — unrelated to the generator. |

## Regenerating

```bash
python3 docs/images/generate_diagrams.py   # rewrites the four .svg files in place
```

**Always edit the script, never the SVGs by hand.** The SVGs are build output. Tweak a
coordinate/label/colour in `generate_diagrams.py` and re-run. After regenerating, preview
(see below) and confirm the XML is still well-formed:

```bash
python3 -c "import xml.dom.minidom,glob; [xml.dom.minidom.parse(f) for f in glob.glob('docs/images/*.svg')]"
```

## Why hand-built SVG (not Mermaid)

Mermaid renders natively on GitHub but only does boxy, linear flowcharts — it can't do the
radial/orbital/constellation layouts these diagrams use. Hand-authored SVG gives full control
(gradients, glows, curved edges, arbitrary placement) at the cost of doing the geometry
yourself. That trade was worth it here; it won't be for a throwaway flowchart.

## GitHub rendering constraints (the hard limits)

- **Referenced as `<img src="docs/images/x.svg">`, not inline.** GitHub's Markdown sanitizer
  strips inline `<svg>`. Repo-relative `<img>`/`<picture>` render fine.
- **No external anything.** No web fonts, no external CSS/JS, no remote images. Use a **system
  font stack** (`Inter, Segoe UI, Helvetica, Arial, sans-serif`) — whatever the viewer has.
- **Escape `&`, `<`, `>` in text.** `html.escape` is applied to every label (see `esc()`); a
  raw `&` (e.g. "Docs & Sheets") produces invalid XML that silently fails to render.
- **Self-contained dark panel = theme-proof.** Rather than shipping light+dark variants via
  `<picture>` + `prefers-color-scheme`, each figure paints its **own** dark rounded panel
  (`panel()`), so it reads as a deliberate figure on both the light and dark GitHub themes.
  One file per diagram, looks right everywhere. Verify on **both** backgrounds anyway.
- **Vector scales losslessly.** The README `<img width="...">` is just display size; pick it
  for layout, not resolution.
- Keep every `<img>`'s **`alt`** descriptive — it's the accessibility text and the fallback
  when an image 404s.

## The design system

One palette and one set of primitives across all four figures so they read as a set. All in
`generate_diagrams.py`:

- **Zones/hues** (`ZONES`): cyan=sources, emerald=core/index/local/tools, amber=orchestrator/
  cloud models, violet=surfaces, rose=autonomous/CLI, blue=web/output. Colour carries meaning;
  don't add hues casually.
- **`panel(w,h,glow)`** — the dark rounded background + vignette + optional core glow.
- **`core(cx,cy,r,lines,sub)`** — the molten "core" disc (orchestrator / hub) with glow rings.
- **`node(cx,cy,text,zone,sub)`** — a rounded chip with a glowing accent dot. Returns
  `(svg, bbox)`; the bbox feeds the edge anchors.
- **`flow(p0,p1,...)`** — a gradient edge with a soft under-glow (fake bloom, since blur
  filters are unreliable on GitHub). `kind="cubic"` for L↔R flows, `kind="arc"` (with `bow`)
  for curves that bow around things.
- **`marker(id,color)` + `zlabel`/`note`** — arrowheads and labels.
- **Anchor helpers** `left/right/top/bot(bbox)` — always connect edges to a node's **border**,
  never its centre.

## Lessons learned (the gotchas)

**Text width is estimated, not measured.** A headless SVG generator has no font metrics, so
`node()` approximates width as `chars × ~7.9px + padding`. Consequences:
- Pad generously and **always preview** — the estimate drifts for bold/wide glyphs.
- Long labels overflow the panel. Fixes: shorten the label, widen the canvas, or move the
  column inward. (Several rounds of "text touches the border" traced to this.)

**Route edges through empty space; anchor at borders.** Overlap complaints ("arrows behind the
nodes") were almost always an edge crossing an unrelated chip. Two fixes that worked:
- Lay clusters in **columns with a gap** so brain-bound edges cross emptiness, not chips
  (architecture's two-column local core).
- Anchor edges to `left/right/top/bot` of the target and **inset the endpoint** (`flow(...,
  arrow=..., gap=N)`) so the arrowhead sits just off the border instead of on the text.

**Arrowheads & direction.**
- One `marker` with `orient="auto-start-reverse"` works as **both** start and end head — set
  `arrow` and `arrow_start` on the same `flow()` for a bidirectional line.
- The head points along the path *at its end*. If a curve looks like it points the wrong way,
  the path direction is wrong, not the marker.

**Circular layouts — clearing wide chips is the whole battle** (`sync_cycle`):
- A chip on a ring occupies an **angular** span ≈ `atan(halfWidth / R)`. Wide chips eat a lot
  of angle. Two adjacent wide chips can leave almost no room for the connector between them.
- `exit_ang()` walks out from a node's centre angle until the point on the ring leaves that
  chip's rectangle — that's how far to inset the arc so it clears the chip.
- **But don't over-inset.** If both ends inset until they nearly meet, you get a zero-length or
  *reversed* arc — which renders as an arrowhead pointing the wrong way with no tail. The
  `MINA` clamp guarantees a minimum arc span (a visible tail) centred in the gap, and keeps
  `start < end` so direction is never flipped. This exact bug produced a "backwards" arrow.
- **Bigger `R` helps**: a larger radius shrinks every chip's angular footprint, buying room.
- An **open** cycle (skip the last→first edge) both removes a crossing near the wide top node
  and reads fine because the numbered badges already imply order.

**Layout must encode meaning.** The services diagram was first drawn as a radial "sunburst"
around the API — and it was rightly called nonsense, because the *angles meant nothing*. It
became honest only when redrawn as **tiers** where vertical position = failure impact
(critical → graceful → external), with explicit fallback arrows and per-tier consequences.
Rule: only go radial/circular when **radius or angle actually encodes something** (the sync
cycle's angle = time/order). Otherwise use bands/columns.

**Preview loop.**
- Serve over `http://localhost` and screenshot with Playwright. `file://` is blocked; **direct
  navigation to a `.svg` can hang** — embed the SVG in a tiny HTML wrapper (`<img src=...>`)
  and screenshot that instead. A wrapper with a hard-cropped, scaled `<img>` is a cheap way to
  zoom into one region (used to confirm the bidirectional arrowheads).
- Check on a **white** and a **dark** background; the panel should look intentional on both.
- Clean up scratch `_preview.html` / screenshots before finishing — don't commit them.

## Related Documents

- [`README.md`](../../README.md) — where these four figures are embedded.
- [`docs/AGENTS.md`](../AGENTS.md) — documentation standards for the repo.
- [`AGENTS.md`](../../AGENTS.md) — top-level project reference (what the diagrams depict).
