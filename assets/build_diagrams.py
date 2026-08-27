#!/usr/bin/env python3
"""Generate the animated README diagrams from the renderer's own design tokens.

    python assets/build_diagrams.py

The README picture and the shipped component are the same thing drawn twice, so
the tokens below are copied from ``packages/renderer/src/styles.css`` and the
node geometry from ``PlanGraph.tsx`` — 224x92 cards, a 22px glyph chip, a
13px title, the mono capability id, and the dot/status/id footer row. If you
restyle the renderer, re-run this; if the two ever disagree, the picture is the
one that is lying.

Output is plain SVG with SMIL animation: no script, no external references, so
it keeps animating through the ``<img>`` boundary GitHub renders it behind.
"""

from __future__ import annotations

import pathlib

# ── Tokens, from packages/renderer/src/styles.css ───────────────────────────
PAPER = "#fdfcf8"
SURFACE = "#ffffff"
SUNKEN = "#f7f4ec"
HAIRLINE = "#efece3"
LINE = "#e8e4d9"
GRAPH_CANVAS = "#f7f8fa"
GRAPH_DOT = "#d8dce4"
INK = "#1a2332"
INK2 = "#3a4556"
INK3 = "#687181"
ACCENT = "#4a7d5b"
ACCENT_WASH = "#edf3ef"
RUN = "#1661ab"
OK = "#6b7a3a"
ERR = "#c03030"
IDLE = "#687181"
EDGE = "#9aa2af"
EDGE_ACTIVE = "#2f6fd0"
EDGE_DONE = "#24734f"

# Node fills and borders are derived the same way the CSS derives them:
# color-mix(status, 2.5-3.5%) over the surface for the fill, 25% for the border.
STATUS = {
    "pending":   {"fill": SURFACE,   "stroke": LINE,      "dot": IDLE, "label": "Pending"},
    "running":   {"fill": "#f7fafc", "stroke": "#c4d8ea", "dot": RUN,  "label": "Running"},
    "succeeded": {"fill": "#fbfcfa", "stroke": "#dadece", "dot": OK,   "label": "Succeeded"},
    "failed":    {"fill": "#fdf8f8", "stroke": "#efcbcb", "dot": ERR,  "label": "Failed"},
}

GLYPH = {"capability": "◈", "workflow": "⛭", "dynamic": "⌘", "review": "⌕", "answer": "✎"}

# ── Geometry, from PlanGraph.tsx / layout.ts ────────────────────────────────
NODE_W, NODE_H = 224, 92
LANE_GAP, LAYER_GAP = 26, 58

# Declared once in a <style> block rather than per-element: family names with
# spaces need quotes, and quotes do not survive an XML attribute cleanly.
FONT_STYLE = """<style>
  .s { font-family: "Inter", ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
  .m { font-family: "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; }
</style>"""


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def keyed(attr: str, values: list[str], times: list[float], dur: float) -> str:
    """A discrete SMIL track. Status changes are steps, not fades."""
    return (
        f'<animate attributeName="{attr}" dur="{dur}s" repeatCount="indefinite" '
        f'calcMode="discrete" values="{";".join(values)}" '
        f'keyTimes="{";".join(f"{t / dur:.4f}" for t in times)}"/>'
    )


def node_svg(x: int, y: int, step: dict, track: list[tuple[float, str]], dur: float) -> str:
    """One plan node, animated across its status track."""
    times = [t for t, _ in track]
    states = [s for _, s in track]
    glyph = GLYPH.get(step["kind"], "◈")
    meta = step.get("capability") or step["kind"].capitalize()

    fills = [STATUS[s]["fill"] for s in states]
    strokes = [STATUS[s]["stroke"] for s in states]
    dots = [STATUS[s]["dot"] for s in states]
    labels = [STATUS[s]["label"] for s in states]

    # The "running" ring, drawn behind the card. Present only while running.
    ring_opacity = ["1" if s == "running" else "0" for s in states]

    parts = [
        f'<g><rect x="{x - 3}" y="{y - 3}" width="{NODE_W + 6}" height="{NODE_H + 6}" rx="15" '
        f'fill="{RUN}" opacity="0">{keyed("opacity", ["0.06" if o == "1" else "0" for o in ring_opacity], times, dur)}</rect>',
        f'<rect x="{x}" y="{y}" width="{NODE_W}" height="{NODE_H}" rx="12" '
        f'fill="{SURFACE}" stroke="{LINE}" stroke-width="1">'
        f'{keyed("fill", fills, times, dur)}{keyed("stroke", strokes, times, dur)}</rect>',
        f'<rect x="{x + 12}" y="{y + 10}" width="22" height="22" rx="6" fill="{SUNKEN}"/>',
        f'<text x="{x + 23}" y="{y + 25}" class="s" font-size="12" fill="{INK2}" '
        f'text-anchor="middle">{glyph}</text>',
        f'<text x="{x + 42}" y="{y + 25}" class="s" font-size="13" font-weight="600" '
        f'fill="{INK}">{esc(step["title"])}</text>',
        f'<text x="{x + 12}" y="{y + 46}" class="m" font-size="10" fill="{INK3}">'
        f'{esc(meta)}</text>',
        f'<circle cx="{x + 15}" cy="{y + 76}" r="3.5" fill="{IDLE}">'
        f'{keyed("fill", dots, times, dur)}</circle>',
    ]
    # The status word is a stack of texts switched by opacity — SMIL cannot
    # animate text content.
    for label in sorted(set(labels)):
        visible = ["1" if item == label else "0" for item in labels]
        parts.append(
            f'<text x="{x + 26}" y="{y + 80}" class="s" font-size="10" '
            f'font-weight="600" fill="{INK3}" opacity="0">'
            f'{keyed("opacity", visible, times, dur)}{label}</text>'
        )
    parts.append(
        f'<text x="{x + 92}" y="{y + 80}" class="m" font-size="10" fill="{INK3}">'
        f'{esc(step["id"])}</text></g>'
    )
    return "".join(parts)


def edge_svg(a: tuple[int, int], b: tuple[int, int], tone_track: list[tuple[float, str]],
             dur: float, long: bool = False) -> str:
    """A top-down edge, coloured by the three tones the renderer uses."""
    (x1, y1), (x2, y2) = a, b
    mid = y1 + (y2 - y1) / 2
    path = f"M{x1},{y1} C{x1},{mid:.0f} {x2},{mid:.0f} {x2},{y2 - 9}"
    times = [t for t, _ in tone_track]
    tones = [t for _, t in tone_track]
    colours = {"idle": EDGE, "active": EDGE_ACTIVE, "done": EDGE_DONE}
    widths = {"idle": "1.6", "active": "2.2", "done": "1.8"}
    dashes = {"idle": "5 4" if long else "none", "active": "6 5", "done": "5 4" if long else "none"}

    flow = (
        f'<animate attributeName="stroke-dashoffset" dur="0.9s" repeatCount="indefinite" '
        f'values="0;-22"/>'
    )
    head = "".join(
        f'<path d="M{x2 - 5},{y2 - 9} L{x2},{y2} L{x2 + 5},{y2 - 9} Z" fill="{colours[tone]}" '
        f'opacity="0">{keyed("opacity", ["1" if t == tone else "0" for t in tones], times, dur)}</path>'
        for tone in sorted(set(tones))
    )
    return (
        f'<path d="{path}" fill="none" stroke="{EDGE}" stroke-width="1.6" stroke-linecap="round">'
        f'{keyed("stroke", [colours[t] for t in tones], times, dur)}'
        f'{keyed("stroke-width", [widths[t] for t in tones], times, dur)}'
        f'{keyed("stroke-dasharray", [dashes[t] for t in tones], times, dur)}'
        f"{flow}</path>{head}"
    )


# ── The figure ──────────────────────────────────────────────────────────────

DUR = 14.0

STEPS = {
    "qc":      {"id": "qc", "title": "Quality control", "kind": "capability", "capability": "gwas.qc"},
    "pca":     {"id": "pca", "title": "Ancestry axes", "kind": "capability", "capability": "gwas.pca"},
    "kinship": {"id": "kinship", "title": "Relatedness matrix", "kind": "capability", "capability": "gwas.kinship"},
    "assoc":   {"id": "assoc", "title": "Structure-aware scan", "kind": "capability", "capability": "gwas.associate"},
    "correct": {"id": "correct", "title": "Multiple testing", "kind": "capability", "capability": "gwas.correct"},
}

# (start, status) pairs. Everything before the first entry is that first status.
TRACKS = {
    "qc":      [(0, "pending"), (1.5, "running"), (3.5, "succeeded")],
    "pca":     [(0, "pending"), (3.8, "running"), (6.0, "succeeded")],
    "kinship": [(0, "pending"), (3.8, "running"), (7.0, "succeeded")],
    "assoc":   [(0, "pending"), (7.3, "running"), (9.6, "succeeded")],
    "correct": [(0, "pending"), (9.9, "running"), (11.6, "succeeded")],
}

NARRATION = [
    (0.0, 1.5, ACCENT, "revision 2 published · 9 steps · validated before anything ran"),
    (1.5, 3.5, RUN, "quality control running · the only step with no unmet dependency"),
    (3.5, 6.0, OK, "qc succeeded → ancestry and relatedness dispatched together"),
    (6.0, 7.3, RUN, "no edge between those two, so the engine did not serialise them"),
    (7.3, 9.9, RUN, "the scan waited for both parents — fan-in, not a race"),
    (9.9, 11.6, RUN, "correcting for multiple testing"),
    (11.6, DUR, OK, "λ = 0.9461 · the null is calibrated and the three hits stand"),
]


def build() -> str:
    header_h, reason_h = 62, 36
    top = header_h + reason_h
    graph_x, graph_y = 316, top + 20

    pos = {
        "qc":      (graph_x + (2 * NODE_W + LANE_GAP - NODE_W) // 2, graph_y),
        "pca":     (graph_x, graph_y + NODE_H + LAYER_GAP),
        "kinship": (graph_x + NODE_W + LANE_GAP, graph_y + NODE_H + LAYER_GAP),
        "assoc":   (graph_x + (2 * NODE_W + LANE_GAP - NODE_W) // 2, graph_y + 2 * (NODE_H + LAYER_GAP)),
        "correct": (graph_x + (2 * NODE_W + LANE_GAP - NODE_W) // 2, graph_y + 3 * (NODE_H + LAYER_GAP)),
    }
    width = 820
    height = pos["correct"][1] + NODE_H + 26

    def bottom(key: str) -> tuple[int, int]:
        x, y = pos[key]
        return x + NODE_W // 2, y + NODE_H

    def top_of(key: str) -> tuple[int, int]:
        x, y = pos[key]
        return x + NODE_W // 2, y

    def tone_track(parent: str, child: str) -> list[tuple[float, str]]:
        """idle → active while the child runs → done once both have succeeded."""
        child_run = next(t for t, s in TRACKS[child] if s == "running")
        child_ok = next(t for t, s in TRACKS[child] if s == "succeeded")
        return [(0, "idle"), (child_run, "active"), (child_ok, "done")]

    edges = [("qc", "pca"), ("qc", "kinship"), ("pca", "assoc"),
             ("kinship", "assoc"), ("assoc", "correct")]

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="The LoomCraft workbench '
        f'rendering revision 2 of an association study: quality control succeeds, ancestry '
        f'axes and the relatedness matrix are dispatched together because neither depends on '
        f'the other, the structure-aware scan waits for both, and multiple-testing correction '
        f'follows.">',
        "<title>Revision 2 of the association plan, executing</title>",
        FONT_STYLE + "",

        # Page + chrome
        f'<rect width="{width}" height="{height}" rx="12" fill="{PAPER}"/>',
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="11.5" '
        f'fill="none" stroke="{HAIRLINE}"/>',
        f'<rect x="1" y="1" width="{width - 2}" height="{header_h}" fill="{SURFACE}"/>',
        f'<line x1="0" y1="{header_h}" x2="{width}" y2="{header_h}" stroke="{HAIRLINE}"/>',

        # Revision badge, step count, goal
        f'<rect x="20" y="14" width="34" height="18" rx="6" fill="{ACCENT_WASH}"/>',
        f'<text x="37" y="27" class="m" font-size="10.5" font-weight="700" '
        f'fill="{ACCENT}" text-anchor="middle">R2</text>',
        f'<text x="62" y="27" class="s" font-size="11" fill="{INK3}">9 steps</text>',
        f'<text x="20" y="48" class="s" font-size="12" font-weight="600" fill="{INK}">'
        f'Find markers associated with salt tolerance in the uploaded cohort.</text>',

        # Revision switcher, as the renderer draws it
        f'<rect x="716" y="12" width="86" height="24" rx="8" fill="{SUNKEN}" stroke="{HAIRLINE}"/>',
        f'<text x="737" y="28" class="m" font-size="10.5" font-weight="600" '
        f'fill="{INK3}" text-anchor="middle">R1</text>',
        f'<rect x="757" y="15" width="42" height="18" rx="6" fill="{SURFACE}"/>',
        f'<text x="778" y="28" class="m" font-size="10.5" font-weight="700" '
        f'fill="{ACCENT}" text-anchor="middle">R2</text>',

        # Replan reason bar
        f'<rect x="1" y="{header_h}" width="{width - 2}" height="{reason_h}" fill="{ACCENT_WASH}"/>',
        f'<line x1="0" y1="{top}" x2="{width}" y2="{top}" stroke="{HAIRLINE}"/>',
        f'<text x="20" y="{header_h + 23}" class="s" font-size="11" fill="{INK2}">'
        f'<tspan font-weight="600" fill="{ACCENT}">Replanned: </tspan>'
        f'λ = 2.8024 in revision 1 — the statistics are inflated genome-wide, which is '
        f'population structure rather than signal.</text>',

        # The graph pane: cool grey, 20px dot grid
        f'<defs><pattern id="dots" width="20" height="20" patternUnits="userSpaceOnUse">'
        f'<circle cx="1" cy="1" r="1" fill="{GRAPH_DOT}"/></pattern></defs>',
        f'<rect x="1" y="{top}" width="{width - 2}" height="{height - top - 1}" fill="{GRAPH_CANVAS}"/>',
        f'<rect x="1" y="{top}" width="{width - 2}" height="{height - top - 1}" fill="url(#dots)"/>',
    ]

    # The conversation overlay the workbench floats over the canvas.
    card_y = top + 26
    out += [
        f'<rect x="20" y="{card_y}" width="272" height="86" rx="14" fill="{SURFACE}" '
        f'stroke="{LINE}"/>',
        f'<text x="36" y="{card_y + 22}" class="s" font-size="9.5" font-weight="600" '
        f'fill="{INK3}" letter-spacing="0.8">WHAT THE USER ASKED</text>',
        f'<text x="36" y="{card_y + 42}" class="s" font-size="11" fill="{INK}">'
        f'Which markers are associated with salt</text>',
        f'<text x="36" y="{card_y + 58}" class="s" font-size="11" fill="{INK}">'
        f'tolerance in this cohort?</text>',
        f'<text x="36" y="{card_y + 76}" class="m" font-size="9.5" fill="{INK3}">'
        f'120 samples · 150 markers</text>',

        f'<rect x="20" y="{card_y + 104}" width="272" height="118" rx="14" fill="#fffaf3" '
        f'stroke="{ACCENT}" stroke-opacity="0.25"/>',
        f'<text x="36" y="{card_y + 126}" class="s" font-size="9.5" font-weight="600" '
        f'fill="{ACCENT}" letter-spacing="0.6">WHAT THE AGENT CHANGED</text>',
        f'<rect x="190" y="{card_y + 114}" width="26" height="15" rx="5" fill="{ACCENT_WASH}"/>',
        f'<text x="203" y="{card_y + 125}" class="m" font-size="9" font-weight="700" '
        f'fill="{ACCENT}" text-anchor="middle">R2</text>',
        f'<text x="36" y="{card_y + 146}" class="s" font-size="11" fill="{INK2}">'
        f'The first scan was not wrong about the</text>',
        f'<text x="36" y="{card_y + 162}" class="s" font-size="11" fill="{INK2}">'
        f'arithmetic. It was wrong about the model:</text>',
        f'<text x="36" y="{card_y + 178}" class="s" font-size="11" fill="{INK2}">'
        f'ancestry moves the phenotype and most</text>',
        f'<text x="36" y="{card_y + 194}" class="s" font-size="11" fill="{INK2}">'
        f'allele frequencies at once.</text>',
        f'<text x="36" y="{card_y + 212}" class="m" font-size="9.5" fill="{ACCENT}">'
        f'λ 2.8024 → 0.9461 · 8 hits → 3</text>',
    ]

    for parent, child in edges:
        out.append(edge_svg(bottom(parent), top_of(child), tone_track(parent, child), DUR))

    for key, step in STEPS.items():
        x, y = pos[key]
        out.append(node_svg(x, y, step, TRACKS[key], DUR))

    # Narration track, bottom-left under the cards.
    narration_y = height - 18
    for start, end, colour, text in NARRATION:
        times = [0.0, start, end] if start > 0 else [0.0, end]
        values = ["0", "1", "0"] if start > 0 else ["1", "0"]
        out.append(
            f'<text x="20" y="{narration_y}" class="m" font-size="10" fill="{INK3}" '
            f'opacity="0">{keyed("opacity", values, times, DUR)}'
            f'<tspan fill="{colour}">▸ </tspan>{esc(text)}</text>'
        )

    out.append("</svg>")
    return "\n".join(out)


if __name__ == "__main__":
    target = pathlib.Path(__file__).parent / "plan-execution.svg"
    target.write_text(build())
    print(f"wrote {target} ({target.stat().st_size:,} bytes)")
