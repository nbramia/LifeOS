#!/usr/bin/env python3
"""Generate the LifeOS README architecture diagrams as self-contained SVGs.

Run:  python3 docs/images/generate_diagrams.py
Writes architecture.svg, query-pipeline.svg, sync-cycle.svg, services.svg into
this directory (the four figures embedded in the top-level README).

No dependencies beyond the Python standard library. Each diagram is a single
self-contained SVG with its own dark "panel" background, so it renders as an
intentional figure on either a light or dark GitHub theme without needing a
<picture>/prefers-color-scheme swap. See AGENTS.md in this directory for the
design rules these helpers encode and the constraints that shaped them.
"""

import math
import html
import os

# Output next to this script — portable, no hardcoded paths.
OUT = os.path.dirname(os.path.abspath(__file__))

# ---- palette -------------------------------------------------------------
TXT = "#e8ecf8"  # primary label text
MUT = "#8b96b5"  # muted / secondary text
# Each "zone" of a diagram gets one accent hue. Reused across all four figures
# so they read as one system.
ZONES = {
    "src": "#22d3ee",  # cyan   — data sources / inputs
    "core": "#34d399",  # emerald — ingest/index, local models, tools
    "brain": "#fbbf24",  # amber  — the orchestrator / cloud models
    "surf": "#a78bfa",  # violet — surfaces
    "auto": "#fb7185",  # rose   — autonomous / CLI engines
    "web": "#38bdf8",  # blue   — web + output
}
FONT = "Inter, Segoe UI, Helvetica, Arial, sans-serif"


def esc(s):
    return html.escape(str(s), quote=True)


def defs():
    """Shared <defs>: panel background, vignette, core-disc + per-zone gradients."""
    d = """<defs>
<linearGradient id="panelBg" x1="0" y1="0" x2="0" y2="1">
  <stop offset="0" stop-color="#0d1226"/><stop offset="0.55" stop-color="#0a0e20"/><stop offset="1" stop-color="#080b18"/></linearGradient>
<linearGradient id="panelStroke" x1="0" y1="0" x2="1" y2="1">
  <stop offset="0" stop-color="#38bdf8" stop-opacity="0.55"/><stop offset="0.5" stop-color="#a78bfa" stop-opacity="0.35"/><stop offset="1" stop-color="#fb7185" stop-opacity="0.45"/></linearGradient>
<radialGradient id="vignette" cx="0.5" cy="0.42" r="0.75">
  <stop offset="0" stop-color="#141b3a" stop-opacity="0.55"/><stop offset="0.6" stop-color="#0b1024" stop-opacity="0"/><stop offset="1" stop-color="#05070f" stop-opacity="0.5"/></radialGradient>
<radialGradient id="brainGlow" cx="0.5" cy="0.5" r="0.5">
  <stop offset="0" stop-color="#fbbf24" stop-opacity="0.26"/><stop offset="0.4" stop-color="#a855f7" stop-opacity="0.12"/><stop offset="1" stop-color="#a855f7" stop-opacity="0"/></radialGradient>
<radialGradient id="coreDisc" cx="0.42" cy="0.35" r="0.85">
  <stop offset="0" stop-color="#fde68a"/><stop offset="0.35" stop-color="#fbbf24"/><stop offset="0.7" stop-color="#b45309"/><stop offset="1" stop-color="#7c2d12"/></radialGradient>"""
    for k, c in ZONES.items():
        d += f'''
<radialGradient id="dot-{k}" cx="0.5" cy="0.5" r="0.5"><stop offset="0" stop-color="{c}"/><stop offset="0.55" stop-color="{c}" stop-opacity="0.85"/><stop offset="1" stop-color="{c}" stop-opacity="0"/></radialGradient>
<radialGradient id="zone-{k}" cx="0.5" cy="0.5" r="0.5"><stop offset="0" stop-color="{c}" stop-opacity="0.11"/><stop offset="1" stop-color="{c}" stop-opacity="0"/></radialGradient>'''
    d += "\n</defs>"
    return d


def panel(w, h, glow=None):
    """Rounded dark panel + vignette. Optional (x,y,r) soft glow behind the core."""
    g = ""
    if glow:
        gx, gy, gr = glow
        g = f'<circle cx="{gx}" cy="{gy}" r="{gr}" fill="url(#brainGlow)"/>'
    return (
        f'<rect x="1.5" y="1.5" width="{w - 3}" height="{h - 3}" rx="22" fill="url(#panelBg)" stroke="url(#panelStroke)" stroke-width="1.5"/>'
        f'<rect x="1.5" y="1.5" width="{w - 3}" height="{h - 3}" rx="22" fill="url(#vignette)"/>{g}'
    )


def marker(mid, color):
    """Solid-colour arrowhead. orient=auto-start-reverse so it works as start OR end."""
    return (
        f'<marker id="{mid}" viewBox="0 0 10 10" refX="8.5" refY="5" markerWidth="6.5" markerHeight="6.5" '
        f'orient="auto-start-reverse"><path d="M0.5 1 L9 5 L0.5 9 Z" fill="{color}"/></marker>'
    )


def svg_open(w, h):
    return f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}" font-family="{FONT}">'


def write(name, body):
    with open(os.path.join(OUT, name), "w") as f:
        f.write(body)
    print("wrote", name)


# ---- path geometry -------------------------------------------------------
def cubic(p0, p1, t=0.5):
    """Horizontal-tangent cubic bezier — the smooth 'flow' curve for LR edges."""
    x0, y0 = p0
    x1, y1 = p1
    dx = (x1 - x0) * t
    return f"M {x0:.1f} {y0:.1f} C {x0 + dx:.1f} {y0:.1f} {x1 - dx:.1f} {y1:.1f} {x1:.1f} {y1:.1f}"


def arc_path(p0, p1, bow=0.22):
    """Quadratic bezier that bows perpendicular to the p0->p1 line (positive = one side)."""
    x0, y0 = p0
    x1, y1 = p1
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    dx, dy = x1 - x0, y1 - y0
    L = math.hypot(dx, dy) or 1
    nx, ny = -dy / L, dx / L
    return f"M {x0:.1f} {y0:.1f} Q {mx + nx * L * bow:.1f} {my + ny * L * bow:.1f} {x1:.1f} {y1:.1f}"


def inset(p0, p1, d):
    """Move p1 back toward p0 by d px (so an arrowhead sits just off the node border)."""
    x0, y0 = p0
    x1, y1 = p1
    dx, dy = x1 - x0, y1 - y0
    L = math.hypot(dx, dy) or 1
    return (x1 - dx / L * d, y1 - dy / L * d)


_gid = 0


def flow(
    p0,
    p1,
    c1,
    c2,
    kind="cubic",
    t=0.5,
    bow=0.22,
    w=2.0,
    op=0.5,
    glow=True,
    arrow=None,
    arrow_start=None,
    gap=8,
):
    """A gradient edge from p0->p1 with an under-glow. arrow / arrow_start are marker ids;
    when set, the endpoint is inset by `gap` so the head doesn't overlap the node."""
    global _gid
    _gid += 1
    gid = f"eg{_gid}"
    if arrow:
        p1 = inset(p0, p1, gap)
    if arrow_start:
        p0 = inset(p1, p0, gap)
    path = cubic(p0, p1, t) if kind == "cubic" else arc_path(p0, p1, bow)
    s = (
        f'<defs><linearGradient id="{gid}" x1="{p0[0]:.0f}" y1="{p0[1]:.0f}" x2="{p1[0]:.0f}" y2="{p1[1]:.0f}" gradientUnits="userSpaceOnUse">'
        f'<stop offset="0" stop-color="{c1}"/><stop offset="1" stop-color="{c2}"/></linearGradient></defs>'
    )
    if glow:
        s += f'<path d="{path}" fill="none" stroke="url(#{gid})" stroke-width="{w * 3:.1f}" stroke-opacity="{op * 0.22:.3f}" stroke-linecap="round"/>'
    mk = (f' marker-end="url(#{arrow})"' if arrow else "") + (
        f' marker-start="url(#{arrow_start})"' if arrow_start else ""
    )
    s += f'<path d="{path}" fill="none" stroke="url(#{gid})" stroke-width="{w}" stroke-opacity="{op}" stroke-linecap="round"{mk}/>'
    return s


# ---- node primitives -----------------------------------------------------
def node(cx, cy, text, zone, sub=None, pad=46):
    """Rounded chip with a glowing accent dot. Returns (svg, bbox) — bbox = (x,y,w,h).
    Width is estimated from character count (~7.9px/char) since there is no text metric."""
    c = ZONES[zone]
    tw = max(len(text), len(sub) if sub else 0) * 7.9 + pad
    h = 46 if not sub else 58
    x = cx - tw / 2
    y = cy - h / 2
    dotx = x + 20
    s = "<g>"
    s += f'<rect x="{x:.1f}" y="{y:.1f}" width="{tw:.1f}" height="{h}" rx="{h / 2 if not sub else 15:.1f}" fill="#0e1530" fill-opacity="0.85" stroke="{c}" stroke-opacity="0.55" stroke-width="1.3"/>'
    s += f'<circle cx="{dotx:.1f}" cy="{cy:.1f}" r="11" fill="url(#dot-{zone})"/><circle cx="{dotx:.1f}" cy="{cy:.1f}" r="3.4" fill="#fff" fill-opacity="0.95"/>'
    tx = dotx + 18
    if sub:
        s += f'<text x="{tx:.1f}" y="{cy - 4:.1f}" font-size="15" font-weight="600" fill="{TXT}">{esc(text)}</text>'
        s += f'<text x="{tx:.1f}" y="{cy + 14:.1f}" font-size="12" fill="{MUT}">{esc(sub)}</text>'
    else:
        s += f'<text x="{tx:.1f}" y="{cy + 5:.1f}" font-size="15" font-weight="600" fill="{TXT}">{esc(text)}</text>'
    s += "</g>"
    return s, (x, y, tw, h)


def core(cx, cy, r, lines, sub=None):
    """The molten 'core' disc (orchestrator / hub) with concentric glow rings."""
    s = f'<circle cx="{cx}" cy="{cy}" r="{r + 30}" fill="#a855f7" fill-opacity="0.10"/>'
    s += f'<circle cx="{cx}" cy="{cy}" r="{r + 12}" fill="none" stroke="#fbbf24" stroke-opacity="0.22" stroke-width="1"/>'
    s += f'<circle cx="{cx}" cy="{cy}" r="{r}" fill="url(#coreDisc)" stroke="#fde68a" stroke-opacity="0.6" stroke-width="1.5"/>'
    n = len(lines)
    y0 = cy - (n - 1) * 9 - (6 if sub else 0)
    for i, ln in enumerate(lines):
        s += f'<text x="{cx}" y="{y0 + i * 19:.0f}" text-anchor="middle" font-size="15" font-weight="800" fill="#3b1e05">{esc(ln)}</text>'
    if sub:
        s += f'<text x="{cx}" y="{y0 + n * 19 + 2:.0f}" text-anchor="middle" font-size="9.5" font-weight="600" fill="#7c2d12">{esc(sub)}</text>'
    return s


def zlabel(x, y, text, color, anchor="start"):
    return f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-size="12.5" font-weight="700" letter-spacing="2.5" fill="{color}" fill-opacity="0.9">{esc(text.upper())}</text>'


def note(x, y, text, color=MUT, anchor="start", size=12):
    return f'<text x="{x}" y="{y}" text-anchor="{anchor}" font-size="{size}" fill="{color}">{esc(text)}</text>'


# bbox anchor helpers — connect edges to a node's border, never its centre.
def right(b):
    x, y, w, h = b
    return (x + w, y + h / 2)


def left(b):
    x, y, w, h = b
    return (x, y + h / 2)


def top(b):
    x, y, w, h = b
    return (x + w / 2, y)


def bot(b):
    x, y, w, h = b
    return (x + w / 2, y + h)


# ============================ 1. ARCHITECTURE ============================
def architecture():
    W, H = 1520, 840
    bx, by = 930, 410  # brain / orchestrator core
    s = [svg_open(W, H), defs(), panel(W, H, glow=(bx, by, 430))]
    # soft zone blobs (drawn first, behind everything)
    s.append('<ellipse cx="205" cy="410" rx="150" ry="330" fill="url(#zone-src)"/>')
    s.append('<ellipse cx="560" cy="405" rx="220" ry="300" fill="url(#zone-core)"/>')
    s.append(
        f'<ellipse cx="{bx}" cy="{by}" rx="150" ry="210" fill="url(#zone-brain)"/>'
    )
    s.append('<ellipse cx="1360" cy="405" rx="150" ry="320" fill="url(#zone-surf)"/>')
    s.append('<ellipse cx="1010" cy="735" rx="290" ry="120" fill="url(#zone-auto)"/>')
    s.append(
        f"<defs>{marker('aC', '#34d399')}{marker('aB', '#fbbf24')}{marker('aS', '#a78bfa')}{marker('aW', '#38bdf8')}{marker('aA', '#fb7185')}</defs>"
    )

    # sources — a gentle left-bulging arc
    srcs = [
        ("Gmail · Calendar · Drive", 130),
        ("iMessage · Calls · Photos", 265),
        ("Slack · WhatsApp · LinkedIn", 400),
        ("Obsidian vault · Granola", 535),
        ("Monarch · Apple Health", 665),
    ]
    src_b = []
    for txt, y in srcs:
        x = 205 - 24 * math.sin((y - 130) / 535 * math.pi)
        g, b = node(x, y, txt, "src")
        src_b.append((g, b))
    # local core pipeline (two columns, so edges to the brain cross empty space)
    sync_g, sync = node(470, 175, "Nightly 7-phase sync", "core")
    store_g, store = node(470, 320, "SQLite · Vault .md", "core")
    resolve_g, resolve = node(470, 465, "Entity resolution", "core")
    vec_g, vec = node(690, 375, "ChromaDB vectors", "core")
    bm_g, bm = node(690, 545, "SQLite FTS5 · BM25", "core")
    # surfaces
    surfs = [
        ("Web /chat — text + voice", "web", 150),
        ("Telegram bots (personas)", "surf", 300),
        ("MCP — Desktop / Code", "surf", 455),
        ("/crm · /agents", "surf", 605),
    ]
    surf_b = []
    for txt, z, y in surfs:
        x = 1360 + 18 * math.sin((y - 150) / 455 * math.pi)
        g, b = node(x, y, txt, z)
        surf_b.append((g, b, z))
    worker_g, worker = node(900, 730, "Agent worker  #agent", "auto")
    sched_g, sched = node(1200, 712, "Scheduler", "auto")

    E = []
    for g, b in src_b:  # sources -> sync (converging fan, no heads)
        E.append(
            flow(
                right(b), left(sync), ZONES["src"], ZONES["core"], t=0.55, w=1.7, op=0.4
            )
        )
    E.append(
        flow(
            bot(sync),
            top(store),
            ZONES["core"],
            ZONES["core"],
            w=1.9,
            op=0.6,
            arrow="aC",
        )
    )
    E.append(
        flow(
            bot(store),
            top(resolve),
            ZONES["core"],
            ZONES["core"],
            w=1.9,
            op=0.6,
            arrow="aC",
        )
    )
    E.append(
        flow(
            right(resolve),
            left(vec),
            ZONES["core"],
            ZONES["core"],
            kind="arc",
            bow=-0.16,
            w=1.7,
            op=0.55,
            arrow="aC",
        )
    )
    E.append(
        flow(
            right(resolve),
            left(bm),
            ZONES["core"],
            ZONES["core"],
            kind="arc",
            bow=0.14,
            w=1.7,
            op=0.55,
            arrow="aC",
        )
    )
    E.append(
        flow(
            right(vec),
            (bx - 70, by - 16),
            ZONES["core"],
            ZONES["brain"],
            t=0.5,
            w=1.9,
            op=0.6,
            arrow="aB",
        )
    )
    E.append(
        flow(
            right(bm),
            (bx - 70, by + 16),
            ZONES["core"],
            ZONES["brain"],
            t=0.5,
            w=1.9,
            op=0.6,
            arrow="aB",
        )
    )
    for g, b, z in surf_b:  # brain -> surfaces
        E.append(
            flow(
                (bx + 66, by),
                left(b),
                ZONES["brain"],
                ZONES[z],
                t=0.5,
                w=1.9,
                op=0.6,
                arrow="aW" if z == "web" else "aS",
            )
        )
    # autonomous loop (delegate down, report back)
    E.append(
        flow(
            (bx - 24, by + 62),
            top(worker),
            ZONES["brain"],
            ZONES["auto"],
            kind="arc",
            bow=0.12,
            w=1.8,
            op=0.55,
            arrow="aA",
        )
    )
    E.append(
        flow(
            right(worker),
            (bx + 40, by + 70),
            ZONES["auto"],
            ZONES["brain"],
            kind="arc",
            bow=0.22,
            w=1.6,
            op=0.4,
            arrow="aB",
        )
    )
    E.append(
        flow(
            (bx + 64, by + 44),
            left(sched),
            ZONES["brain"],
            ZONES["auto"],
            kind="arc",
            bow=0.16,
            w=1.8,
            op=0.5,
            arrow="aA",
        )
    )
    s.extend(E)

    s.append(core(bx, by, 58, ["Agent", "loop"], "Claude · llama"))
    for g, b in src_b:
        s.append(g)
    for g in (sync_g, store_g, resolve_g, vec_g, bm_g):
        s.append(g)
    for g, b, z in surf_b:
        s.append(g)
    s.append(worker_g)
    s.append(sched_g)

    s.append(zlabel(90, 66, "Data sources", ZONES["src"]))
    s.append(zlabel(360, 66, "Ingest · store · index — local", ZONES["core"]))
    s.append(zlabel(bx, 120, "Orchestration", ZONES["brain"], anchor="middle"))
    s.append(note(bx, 138, "hybrid search · RRF", MUT, anchor="middle", size=11))
    s.append(zlabel(1270, 66, "Surfaces", ZONES["surf"]))
    s.append(zlabel(900, 800, "Autonomous", ZONES["auto"]))
    s.append("</svg>")
    write("architecture.svg", "\n".join(s))


# ============================ 2. QUERY PIPELINE ============================
def query_pipeline():
    W, H = 1500, 950
    cx, cy = 730, 372
    s = [svg_open(W, H), defs(), panel(W, H, glow=(cx, cy, 430))]
    # four zones around the core: input(left) tools(top) output(right) models(bottom)
    s.append('<ellipse cx="190" cy="400" rx="180" ry="290" fill="url(#zone-surf)"/>')
    s.append(f'<ellipse cx="{cx}" cy="140" rx="470" ry="135" fill="url(#zone-core)"/>')
    s.append('<ellipse cx="1330" cy="380" rx="170" ry="210" fill="url(#zone-web)"/>')
    s.append('<ellipse cx="720" cy="740" rx="500" ry="220" fill="url(#zone-brain)"/>')
    s.append(
        f"<defs>{marker('qS', '#a78bfa')}{marker('qC', '#34d399')}{marker('qW', '#38bdf8')}{marker('qB', '#fbbf24')}{marker('qR', '#fb7185')}</defs>"
    )

    # LEFT: input surfaces converge into the loop
    ins = [("Web /chat", 210), ("Telegram", 320), ("Voice", 470), ("MCP client", 580)]
    in_b = []
    for txt, y in ins:
        g, b = node(150, y, txt, "surf")
        in_b.append((g, b))
    qx = 470  # convergence point
    E = []
    for g, b in in_b:
        E.append(
            flow(
                right(b), (qx, cy), ZONES["surf"], ZONES["surf"], t=0.5, w=1.7, op=0.45
            )
        )
    E.append(
        flow(
            (qx, cy),
            (cx - 70, cy),
            ZONES["surf"],
            ZONES["brain"],
            t=0.5,
            w=3.0,
            op=0.7,
            arrow="qB",
            gap=6,
        )
    )

    # TOP: intra-query tool loop (floating tools + a round-trip call/return arrow pair)
    tools = [
        ("search_vault", 520, 180),
        ("email", 640, 128),
        ("calendar", 760, 112),
        ("web", 880, 128),
        ("tasks", 1000, 180),
        ("people", 1120, 235),
    ]
    tool_b = []
    for t, x, y in tools:
        g, b = node(x, y, t, "core")
        tool_b.append((g, b))
    E.append(
        flow(
            (cx - 30, cy - 60),
            (cx - 150, 200),
            ZONES["brain"],
            ZONES["core"],
            kind="arc",
            bow=0.18,
            w=2.2,
            op=0.65,
            arrow="qC",
        )
    )
    E.append(
        flow(
            (cx + 150, 205),
            (cx + 34, cy - 58),
            ZONES["core"],
            ZONES["brain"],
            kind="arc",
            bow=0.18,
            w=2.2,
            op=0.6,
            arrow="qB",
        )
    )

    # RIGHT: output returns to the same surface
    resp_g, resp = node(1330, 340, "Response", "web")
    back_g, back = node(1340, 440, "back to same surface", "web", pad=40)
    E.append(
        flow(
            (cx + 70, cy),
            left(resp),
            ZONES["brain"],
            ZONES["web"],
            t=0.55,
            w=3.0,
            op=0.7,
            arrow="qW",
            gap=6,
        )
    )
    E.append(
        flow(
            bot(resp), top(back), ZONES["web"], ZONES["web"], w=1.6, op=0.45, arrow="qW"
        )
    )

    # BOTTOM: model handoff — Haiku is the hub; non-Haiku models stacked vertically
    def mk(mx, myy, txt, z):
        g, b = node(mx, myy, txt, z)
        return {"g": g, "b": b, "z": z}

    local = mk(452, 744, "Local · Gemma", "core")
    haiku = mk(652, 744, "Haiku", "brain")
    sx = 920
    sonnet = mk(sx, 590, "Sonnet", "brain")
    opus = mk(sx, 678, "Opus", "brain")
    ccode = mk(sx, 766, "Claude Code", "surf")
    codex = mk(sx, 854, "Codex", "auto")
    fanout = [(sonnet, "qB"), (opus, "qB"), (ccode, "qS"), (codex, "qR")]
    # agent loop <-> Haiku (bidirectional); agent loop -> Local
    E.append(
        flow(
            (cx - 16, cy + 58),
            top(haiku["b"]),
            ZONES["brain"],
            ZONES["brain"],
            kind="arc",
            bow=-0.05,
            w=2.3,
            op=0.62,
            arrow="qB",
            arrow_start="qB",
        )
    )
    E.append(
        flow(
            (cx - 58, cy + 50),
            top(local["b"]),
            ZONES["brain"],
            ZONES["core"],
            kind="arc",
            bow=-0.16,
            w=1.9,
            op=0.55,
            arrow="qC",
        )
    )
    for m, amk in fanout:  # Haiku -> each non-Haiku model (enters left edge)
        E.append(
            flow(
                right(haiku["b"]),
                left(m["b"]),
                ZONES["brain"],
                ZONES[m["z"]],
                t=0.45,
                w=1.8,
                op=0.55,
                arrow=amk,
            )
        )
    # escalation chain Sonnet -> Opus -> Claude Code (vertical, enters top edge)
    E.append(
        flow(
            bot(sonnet["b"]),
            top(opus["b"]),
            ZONES["brain"],
            ZONES["brain"],
            w=1.8,
            op=0.6,
            arrow="qB",
        )
    )
    E.append(
        flow(
            bot(opus["b"]),
            top(ccode["b"]),
            ZONES["brain"],
            ZONES["surf"],
            w=1.8,
            op=0.6,
            arrow="qS",
        )
    )

    s.extend(E)
    s.append(core(cx, cy, 62, ["Agent", "loop"], "orchestrator"))
    for g, b in in_b:
        s.append(g)
    for g, b in tool_b:
        s.append(g)
    s.append(resp_g)
    s.append(back_g)
    for m in (local, haiku, sonnet, opus, ccode, codex):
        s.append(m["g"])

    s.append(zlabel(150, 88, "Input surfaces", ZONES["surf"]))
    s.append(note(150, 108, "where a query arrives", MUT, size=11.5))
    s.append(zlabel(cx, 52, "Intra-query tool calls", ZONES["core"], anchor="middle"))
    s.append(
        note(
            cx,
            70,
            "looped over multiple rounds within one query",
            MUT,
            anchor="middle",
            size=11.5,
        )
    )
    s.append(zlabel(1360, 268, "Output", ZONES["web"], anchor="middle"))
    s.append(
        zlabel(690, 540, "Model handoff · escalation", ZONES["brain"], anchor="middle")
    )
    s.append(
        note(
            cx,
            924,
            "agent loop runs on local Gemma or cloud Haiku · Haiku escalates to Sonnet / Opus or hands off to Claude Code / Codex",
            MUT,
            anchor="middle",
            size=11.5,
        )
    )
    s.append("</svg>")
    write("query-pipeline.svg", "\n".join(s))


# ============================ 3. SYNC CYCLE ============================
def sync_cycle():
    W, H = 1160, 1010
    cx, cy = 580, 510
    R = 372  # ring radius; larger R => wide chips take less angle
    s = [svg_open(W, H), defs(), panel(W, H, glow=(cx, cy, 430))]
    cols = ["#22d3ee", "#34d399", "#fbbf24", "#a78bfa", "#38bdf8", "#fb7185", "#22d3ee"]
    s.append("<defs>" + "".join(marker(f"r{i}", cols[i]) for i in range(7)) + "</defs>")

    phases = [
        ("1", "Collection", "Gmail · iMessage · Slack"),
        ("2", "Entity", "Link sources to people"),
        ("3", "Relationships", "Score & graph strength"),
        ("4", "Indexing", "ChromaDB + BM25 reindex"),
        ("5", "Content", "Google Docs & Sheets"),
        ("6", "Cleanup", "Auto-hide non-humans"),
        ("7", "Verify", "Consistency checks"),
    ]
    n = 7

    def ang(i):  # phase 1 at top, clockwise
        return -90 + i * 360 / n

    pts = [
        (
            cx + R * math.cos(math.radians(ang(i))),
            cy + R * math.sin(math.radians(ang(i))),
        )
        for i in range(n)
    ]
    s.append(
        f'<circle cx="{cx}" cy="{cy}" r="{R}" fill="none" stroke="#38bdf8" stroke-opacity="0.12" stroke-width="1.5" stroke-dasharray="2 10" stroke-linecap="round"/>'
    )

    def exit_ang(i, d):
        """Walk out from node i's centre angle (direction d = +1/-1) until the point on
        the ring leaves node i's chip rectangle — so a connector arc clears the chip."""
        pcx, pcy = pts[i]
        tw = max(len(phases[i][1]), len(phases[i][2])) * 6.7 + 72
        hw, hh = tw / 2 + 14, 64 / 2 + 14
        step = 0
        while step < 70:
            step += 1
            a = math.radians(ang(i) + d * step)
            X, Y = cx + R * math.cos(a), cy + R * math.sin(a)
            if abs(X - pcx) > hw or abs(Y - pcy) > hh:
                return ang(i) + d * step + d * 3
        return ang(i) + d * step

    # connectors between consecutive phases; skip 7->1 so the cycle stays open (numbers imply order)
    MINA = 7.0  # minimum arc span (degrees) => every arrow keeps a visible tail
    for i in range(n - 1):
        st = exit_ang(i, 1)
        en = exit_ang(i + 1, -1)
        if en - st < MINA:  # wide adjacent chips: centre a short arc in the gap
            mid = (ang(i) + ang(i + 1)) / 2
            st = mid - MINA / 2
            en = mid + MINA / 2
        a0 = math.radians(st)
        a1 = math.radians(en)
        x0, y0 = cx + R * math.cos(a0), cy + R * math.sin(a0)
        x1, y1 = cx + R * math.cos(a1), cy + R * math.sin(a1)
        global _gid
        _gid += 1
        gid = f"sg{_gid}"
        s.append(
            f'<defs><linearGradient id="{gid}" x1="{x0:.0f}" y1="{y0:.0f}" x2="{x1:.0f}" y2="{y1:.0f}" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="{cols[i]}"/><stop offset="1" stop-color="{cols[(i + 1) % 7]}"/></linearGradient></defs>'
        )
        s.append(
            f'<path d="M {x0:.1f} {y0:.1f} A {R} {R} 0 0 1 {x1:.1f} {y1:.1f}" fill="none" stroke="{cols[i]}" stroke-opacity="0.13" stroke-width="7" stroke-linecap="round"/>'
        )
        s.append(
            f'<path d="M {x0:.1f} {y0:.1f} A {R} {R} 0 0 1 {x1:.1f} {y1:.1f}" fill="none" stroke="url(#{gid})" stroke-opacity="0.75" stroke-width="2.4" marker-end="url(#r{i})"/>'
        )

    zc = ["src", "core", "brain", "surf", "web", "auto", "src"]
    for i, (num, title, sub) in enumerate(phases):
        x, y = pts[i]
        z = zc[i]
        c = ZONES[z]
        tw = max(len(title), len(sub)) * 6.7 + 72
        h = 64
        nx, ny = x - tw / 2, y - h / 2
        s.append(
            f'<rect x="{nx:.1f}" y="{ny:.1f}" width="{tw:.1f}" height="{h}" rx="16" fill="#0e1530" fill-opacity="0.92" stroke="{c}" stroke-opacity="0.6" stroke-width="1.4"/>'
        )
        bxn = nx + 30
        s.append(
            f'<circle cx="{bxn:.1f}" cy="{y:.1f}" r="17" fill="url(#dot-{z})"/><circle cx="{bxn:.1f}" cy="{y:.1f}" r="14" fill="#0b1024" stroke="{c}" stroke-opacity="0.8" stroke-width="1.4"/>'
        )
        s.append(
            f'<text x="{bxn:.1f}" y="{y + 5:.1f}" text-anchor="middle" font-size="15" font-weight="800" fill="{c}">{num}</text>'
        )
        s.append(
            f'<text x="{bxn + 26:.1f}" y="{y - 6:.1f}" font-size="15" font-weight="700" fill="{TXT}">{esc(title)}</text>'
        )
        s.append(
            f'<text x="{bxn + 26:.1f}" y="{y + 13:.1f}" font-size="11" fill="{MUT}">{esc(sub)}</text>'
        )

    s.append(core(cx, cy, 72, ["Nightly", "sync"], "7 phases · nightly"))
    s.append("</svg>")
    write("sync-cycle.svg", "\n".join(s))


# ============================ 4. SERVICE RESILIENCE (tiered by failure impact) ============================
def services():
    W, H = 1280, 660
    s = [svg_open(W, H), defs(), panel(W, H, glow=(W / 2, H / 2, 460))]
    RED = "#f87171"
    AMB = "#fbbf24"
    GRN = "#34d399"
    s.append(f"<defs>{marker('aFb', '#fbbf24')}</defs>")

    def schip(x, y, text, color, muted=False):
        tw = len(text) * 8.0 + 50
        h = 44
        nx, ny = x, y - h / 2
        op = 0.6 if not muted else 0.34
        fill = "#0f1732" if not muted else "#0b1022"
        o = f'<rect x="{nx:.1f}" y="{ny:.1f}" width="{tw:.1f}" height="{h}" rx="12" fill="{fill}" fill-opacity="0.92" stroke="{color}" stroke-opacity="{op}" stroke-width="1.3"/>'
        o += f'<circle cx="{nx + 19:.1f}" cy="{y:.1f}" r="5.5" fill="{color}" fill-opacity="{0.95 if not muted else 0.7}"/>'
        o += f'<text x="{nx + 34:.1f}" y="{y + 5:.1f}" font-size="14.5" font-weight="600" fill="{TXT}">{esc(text)}</text>'
        return o, tw

    # tier: (colour, word, sub, severity badge, consequence, y-centre)
    tiers = [
        (
            RED,
            "Critical",
            "local · no fallback",
            "CRITICAL · alerts immediately",
            "if any fail → LifeOS is offline",
            150,
        ),
        (
            AMB,
            "Graceful",
            "degrades to a fallback",
            "WARNING · batched nightly",
            "if it fails → auto-fallback, no outage",
            340,
        ),
        (
            GRN,
            "External",
            "third-party APIs",
            "WARNING / INFO",
            "if it fails → that feature pauses",
            530,
        ),
    ]
    bx0, bx1 = 40, W - 40
    bh = 150
    for col, word, sub, sev, cons, yc in tiers:
        s.append(
            f'<rect x="{bx0}" y="{yc - bh / 2:.0f}" width="{bx1 - bx0}" height="{bh}" rx="18" fill="{col}" fill-opacity="0.055" stroke="{col}" stroke-opacity="0.28" stroke-width="1.3"/>'
        )
        s.append(
            f'<rect x="{bx0}" y="{yc - bh / 2:.0f}" width="7" height="{bh}" rx="3.5" fill="{col}" fill-opacity="0.85"/>'
        )  # accent bar
        s.append(
            f'<text x="{bx0 + 34}" y="{yc - 16:.0f}" font-size="21" font-weight="800" fill="{col}">{esc(word)}</text>'
        )
        s.append(
            f'<text x="{bx0 + 34}" y="{yc + 6:.0f}" font-size="12.5" fill="{MUT}">{esc(sub)}</text>'
        )
        pw = len(sev) * 6.3 + 26  # severity pill
        s.append(
            f'<rect x="{bx0 + 34}" y="{yc + 18:.0f}" width="{pw:.0f}" height="24" rx="12" fill="{col}" fill-opacity="0.12" stroke="{col}" stroke-opacity="0.5" stroke-width="1"/>'
        )
        s.append(f'<circle cx="{bx0 + 48:.0f}" cy="{yc + 30:.0f}" r="4" fill="{col}"/>')
        s.append(
            f'<text x="{bx0 + 58}" y="{yc + 34:.0f}" font-size="11" font-weight="600" fill="{col}" fill-opacity="0.95">{esc(sev)}</text>'
        )
        s.append(
            f'<text x="{bx1 - 24}" y="{yc - bh / 2 + 26:.0f}" text-anchor="end" font-size="12.5" font-style="italic" fill="{MUT}">{esc(cons)}</text>'
        )

    x = 380  # tier 1 chips
    for t in ["ChromaDB", "Embedding model", "Vault filesystem"]:
        o, w = schip(x, 150, t, RED)
        s.append(o)
        x += w + 26
    x = 380  # tier 2: primary -> fallback pairs
    for prim, fb in [
        ("Intent classifier", "Regex patterns"),
        ("BM25 keyword", "Vector-only"),
    ]:
        o, w = schip(x, 340, prim, AMB)
        s.append(o)
        ax = x + w
        o2, w2 = schip(ax + 70, 340, fb, AMB, muted=True)
        s.append(
            f'<path d="M {ax + 8:.0f} 340 L {ax + 62:.0f} 340" fill="none" stroke="{AMB}" stroke-opacity="0.6" stroke-width="1.6" stroke-dasharray="4 4" marker-end="url(#aFb)"/>'
        )
        s.append(
            f'<text x="{ax + 35:.0f}" y="322" text-anchor="middle" font-size="9.5" fill="{MUT}">falls back to</text>'
        )
        s.append(o2)
        x = ax + 70 + w2 + 64
    x = 380  # tier 3 chips
    for t in ["Google APIs", "Slack", "Monarch", "LLM backend", "whisper-relay :9788"]:
        o, w = schip(x, 530, t, GRN)
        s.append(o)
        x += w + 20

    s.append("</svg>")
    write("services.svg", "\n".join(s))


if __name__ == "__main__":
    architecture()
    query_pipeline()
    sync_cycle()
    services()
