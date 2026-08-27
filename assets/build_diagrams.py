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


# ── Shared chrome ───────────────────────────────────────────────────────────

def frame(width: int, height: int, title: str, aria: str) -> list[str]:
    """Page, border and font declarations — every figure opens with these."""
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{esc(aria)}">',
        f"<title>{esc(title)}</title>",
        FONT_STYLE,
        f'<rect width="{width}" height="{height}" rx="12" fill="{PAPER}"/>',
        f'<rect x="0.5" y="0.5" width="{width - 1}" height="{height - 1}" rx="11.5" '
        f'fill="none" stroke="{HAIRLINE}"/>',
    ]


def caption(x: int, y: int, text: str, colour: str = INK3, size: float = 10,
            anchor: str = "start", mono: bool = True) -> str:
    cls = "m" if mono else "s"
    return (
        f'<text x="{x}" y="{y}" class="{cls}" font-size="{size}" fill="{colour}" '
        f'text-anchor="{anchor}">{esc(text)}</text>'
    )


def heading(x: int, y: int, text: str, colour: str = INK) -> str:
    return (
        f'<text x="{x}" y="{y}" class="s" font-size="12.5" font-weight="600" '
        f'fill="{colour}">{esc(text)}</text>'
    )


def eyebrow(x: int, y: int, text: str, colour: str) -> str:
    return (
        f'<text x="{x}" y="{y}" class="s" font-size="9.5" font-weight="600" '
        f'fill="{colour}" letter-spacing="0.7">{esc(text)}</text>'
    )


def pill(x: int, y: int, w: int, h: int, text: str, stroke: str, fill: str,
         ink: str = INK, size: float = 11.5, mono: bool = True) -> str:
    cls = "m" if mono else "s"
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{h // 2}" fill="{fill}" '
        f'stroke="{stroke}" stroke-width="1.4"/>'
        f'<text x="{x + w // 2}" y="{y + h // 2 + 4}" class="{cls}" font-size="{size}" '
        f'font-weight="600" fill="{ink}" text-anchor="middle">{esc(text)}</text>'
    )


def arrow(x1: float, y1: float, x2: float, y2: float, colour: str = EDGE,
          width: float = 1.6, dashed: bool = False, curve: float = 0.0) -> str:
    """A straight or gently curved arrow with a solid head at the target end."""
    import math as _math

    dx, dy = x2 - x1, y2 - y1
    length = _math.hypot(dx, dy) or 1.0
    ux, uy = dx / length, dy / length
    tip_x, tip_y = x2, y2
    base_x, base_y = x2 - ux * 8, y2 - uy * 8
    px, py = -uy * 4.4, ux * 4.4
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    if curve:
        mx, my = (x1 + x2) / 2 - uy * curve, (y1 + y2) / 2 + ux * curve
        path = f"M{x1:.1f},{y1:.1f} Q{mx:.1f},{my:.1f} {base_x:.1f},{base_y:.1f}"
    else:
        path = f"M{x1:.1f},{y1:.1f} L{base_x:.1f},{base_y:.1f}"
    return (
        f'<path d="{path}" fill="none" stroke="{colour}" stroke-width="{width}" '
        f'stroke-linecap="round"{dash}/>'
        f'<path d="M{base_x + px:.1f},{base_y + py:.1f} L{tip_x:.1f},{tip_y:.1f} '
        f'L{base_x - px:.1f},{base_y - py:.1f} Z" fill="{colour}"/>'
    )


def panel(x: int, y: int, w: int, h: int, stroke: str = LINE, fill: str = SURFACE,
          radius: int = 12, dashed: bool = False) -> str:
    dash = ' stroke-dasharray="5 5"' if dashed else ""
    return (
        f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" '
        f'stroke="{stroke}" stroke-width="1.3"{dash}/>'
    )


# ════════════════════════════════════════════════════════════════════════════
#  Figure 1 — the hero: revision 1 runs, is found wanting, and is replaced
# ════════════════════════════════════════════════════════════════════════════

DUR = 18.0
SWAP = 9.6          # the moment revision 2 is published

R1_STEPS = {
    "qc":      {"id": "qc", "title": "Quality control", "kind": "capability", "capability": "gwas.qc"},
    "assoc":   {"id": "assoc", "title": "Association scan", "kind": "capability", "capability": "gwas.associate"},
    "correct": {"id": "correct", "title": "Multiple testing", "kind": "capability", "capability": "gwas.correct"},
    "review":  {"id": "review", "title": "Is the model calibrated?", "kind": "review", "capability": None},
    "answer":  {"id": "answer", "title": "Report the loci", "kind": "answer", "capability": None},
}

R2_STEPS = {
    "qc":       {"id": "qc", "title": "Quality control", "kind": "capability", "capability": "gwas.qc"},
    "pca":      {"id": "pca", "title": "Ancestry axes", "kind": "capability", "capability": "gwas.pca"},
    "kinship":  {"id": "kinship", "title": "Relatedness matrix", "kind": "capability", "capability": "gwas.kinship"},
    "assoc":    {"id": "assoc", "title": "Structure-aware scan", "kind": "capability", "capability": "gwas.associate"},
    "correct":  {"id": "correct", "title": "Multiple testing", "kind": "capability", "capability": "gwas.correct"},
    "annotate": {"id": "annotate", "title": "Annotate the hits", "kind": "capability", "capability": "gwas.annotate"},
}

R1_TRACKS = {
    "qc":      [(0, "pending"), (1.0, "running"), (2.6, "succeeded")],
    "assoc":   [(0, "pending"), (2.9, "running"), (4.8, "succeeded")],
    "correct": [(0, "pending"), (5.1, "running"), (6.6, "succeeded")],
    "review":  [(0, "pending"), (6.9, "running"), (8.2, "succeeded")],
    "answer":  [(0, "pending")],          # never reached — the plan is replaced
}

R2_TRACKS = {
    "qc":       [(0, "pending"), (SWAP + 0.3, "running"), (SWAP + 1.4, "succeeded")],
    "pca":      [(0, "pending"), (SWAP + 1.7, "running"), (SWAP + 3.5, "succeeded")],
    "kinship":  [(0, "pending"), (SWAP + 1.7, "running"), (SWAP + 4.1, "succeeded")],
    "assoc":    [(0, "pending"), (SWAP + 4.4, "running"), (SWAP + 6.0, "succeeded")],
    "correct":  [(0, "pending"), (SWAP + 6.3, "running"), (SWAP + 7.3, "succeeded")],
    "annotate": [(0, "pending"), (SWAP + 7.6, "running"), (SWAP + 8.2, "succeeded")],
}

NARRATION = [
    (0.0, 1.0, ACCENT, "revision 1 published · 5 steps · validated before anything ran"),
    (1.0, 2.9, RUN, "quality control · 150 markers in, 148 out"),
    (2.9, 5.1, RUN, "one scan, one marker at a time — nothing to run in parallel"),
    (5.1, 6.9, RUN, "correcting for multiple testing · 8 markers survive"),
    (6.9, 8.2, RUN, "review: reading the genomic inflation factor off the artifact"),
    (8.2, SWAP, ERR, "λ = 2.8024 — every statistic is inflated, not eight loci. 5 of the 8 are ancestry."),
    (SWAP, SWAP + 1.7, ACCENT, "revision 2 published · the review step rewrote the plan, not the answer"),
    (SWAP + 1.7, SWAP + 4.4, RUN, "ancestry and relatedness dispatched together — no edge between them"),
    (SWAP + 4.4, SWAP + 6.3, RUN, "the scan waited for both parents · fan-in, not a race"),
    (SWAP + 6.3, SWAP + 7.3, RUN, "correcting for multiple testing"),
    (SWAP + 7.3, DUR, OK, "λ = 0.9461 · 3 markers survive, and all three are real"),
]


def hero() -> str:
    header_h, reason_h = 62, 36
    top = header_h + reason_h
    graph_y = top + 20
    lane = NODE_W + LANE_GAP                       # centre-to-centre of layer 1
    graph_x = 316
    centre = graph_x + (2 * NODE_W + LANE_GAP - NODE_W) // 2

    def row(layer: int) -> int:
        return graph_y + layer * (NODE_H + LAYER_GAP)

    r1_pos = {key: (centre, row(index)) for index, key in enumerate(R1_STEPS)}
    r2_pos = {
        "qc": (centre, row(0)),
        "pca": (graph_x, row(1)),
        "kinship": (graph_x + lane, row(1)),
        "assoc": (centre, row(2)),
        "correct": (centre, row(3)),
        "annotate": (centre, row(4)),
    }
    r1_edges = [("qc", "assoc"), ("assoc", "correct"), ("correct", "review"), ("review", "answer")]
    r2_edges = [("qc", "pca"), ("qc", "kinship"), ("pca", "assoc"), ("kinship", "assoc"),
                ("assoc", "correct"), ("correct", "annotate")]

    width = 820
    height = row(4) + NODE_H + 46

    def revision_group(steps, pos, edges, tracks, visible_from, visible_to) -> str:
        """One revision's whole graph, shown only while that revision is current."""
        def bottom(key):
            x, y = pos[key]
            return x + NODE_W // 2, y + NODE_H

        def top_of(key):
            x, y = pos[key]
            return x + NODE_W // 2, y

        def tone(child):
            entries = dict((status, t) for t, status in tracks[child])
            if "running" not in entries:
                return [(0, "idle")]
            return [(0, "idle"), (entries["running"], "active"),
                    *([(entries["succeeded"], "done")] if "succeeded" in entries else [])]

        body = [edge_svg(bottom(a), top_of(b), tone(b), DUR) for a, b in edges]
        body += [node_svg(*pos[key], steps[key], tracks[key], DUR) for key in steps]

        times = [0.0, visible_from, visible_to] if visible_from > 0 else [0.0, visible_to]
        values = ["0", "1", "0"] if visible_from > 0 else ["1", "0"]
        if visible_to >= DUR:
            times, values = times[:-1], values[:-1]
        return (
            f'<g opacity="0">{keyed("opacity", values, times, DUR)}'
            + "".join(body) + "</g>"
        )

    out = frame(
        width, height,
        "An association plan replacing itself",
        "Revision 1 of an association plan runs to a review step, which reads a genomic "
        "inflation factor of 2.80 off the artifact and concludes the model is confounded. "
        "Revision 2 replaces it with a structure-aware plan in which ancestry axes and a "
        "relatedness matrix are dispatched together, and the corrected scan returns a "
        "calibrated inflation factor of 0.95.",
    )

    # Header chrome
    out += [
        f'<rect x="1" y="1" width="{width - 2}" height="{header_h}" fill="{SURFACE}"/>',
        f'<line x1="0" y1="{header_h}" x2="{width}" y2="{header_h}" stroke="{HAIRLINE}"/>',
        heading(20, 48, "Find markers associated with salt tolerance in the uploaded cohort."),
    ]
    # Badge + step count flip at the swap.
    for label, count, start, end in (("R1", "5 steps", 0.0, SWAP), ("R2", "9 steps", SWAP, DUR)):
        times = [0.0, start, end] if start > 0 else [0.0, end]
        values = ["0", "1", "0"] if start > 0 else ["1", "0"]
        if end >= DUR:
            times, values = times[:-1], values[:-1]
        out.append(
            f'<g opacity="0">{keyed("opacity", values, times, DUR)}'
            f'<rect x="20" y="14" width="34" height="18" rx="6" fill="{ACCENT_WASH}"/>'
            f'<text x="37" y="27" class="m" font-size="10.5" font-weight="700" fill="{ACCENT}" '
            f'text-anchor="middle">{label}</text>'
            f'<text x="62" y="27" class="s" font-size="11" fill="{INK3}">{count}</text></g>'
        )

    # Revision switcher: R2 only appears once it exists.
    out += [
        f'<rect x="716" y="12" width="86" height="24" rx="8" fill="{SUNKEN}" stroke="{HAIRLINE}"/>',
        f'<g opacity="1">{keyed("opacity", ["1", "0"], [0.0, SWAP], DUR)}'
        f'<rect x="718" y="15" width="42" height="18" rx="6" fill="{SURFACE}"/></g>',
        f'<g opacity="0">{keyed("opacity", ["0", "1"], [0.0, SWAP], DUR)}'
        f'<rect x="757" y="15" width="42" height="18" rx="6" fill="{SURFACE}"/></g>',
        f'<text x="739" y="28" class="m" font-size="10.5" font-weight="700" fill="{ACCENT}" '
        f'text-anchor="middle">R1</text>',
        f'<text x="778" y="28" class="m" font-size="10.5" font-weight="600" fill="{INK3}" '
        f'text-anchor="middle">R2</text>',
    ]

    # The bar under the header: plan summary while R1 is current, replan reason after.
    out += [
        f'<rect x="1" y="{header_h}" width="{width - 2}" height="{reason_h}" fill="{SUNKEN}">'
        f'{keyed("fill", [SUNKEN, ACCENT_WASH], [0.0, SWAP], DUR)}</rect>',
        f'<line x1="0" y1="{top}" x2="{width}" y2="{top}" stroke="{HAIRLINE}"/>',
        f'<text x="20" y="{header_h + 23}" class="s" font-size="11" fill="{INK2}" opacity="1">'
        f'{keyed("opacity", ["1", "0"], [0.0, SWAP], DUR)}'
        f'QC the genotypes, scan every marker, correct for multiple testing.</text>',
        f'<text x="20" y="{header_h + 23}" class="s" font-size="11" fill="{INK2}" opacity="0">'
        f'{keyed("opacity", ["0", "1"], [0.0, SWAP], DUR)}'
        f'<tspan font-weight="600" fill="{ACCENT}">Replanned: </tspan>'
        f'λ = 2.8024 in revision 1 — the statistics are inflated genome-wide, which is '
        f'population structure rather than signal.</text>',
    ]

    # Graph pane
    out += [
        f'<defs><pattern id="dots" width="20" height="20" patternUnits="userSpaceOnUse">'
        f'<circle cx="1" cy="1" r="1" fill="{GRAPH_DOT}"/></pattern></defs>',
        f'<rect x="1" y="{top}" width="{width - 2}" height="{height - top - 1}" fill="{GRAPH_CANVAS}"/>',
        f'<rect x="1" y="{top}" width="{width - 2}" height="{height - top - 1}" fill="url(#dots)"/>',
    ]

    # The side cards.
    card_y = top + 34
    out += [
        panel(20, card_y, 272, 84),
        eyebrow(36, card_y + 22, "WHAT THE USER ASKED", INK3),
        caption(36, card_y + 42, "Which markers are associated with salt", INK, 11, mono=False),
        caption(36, card_y + 58, "tolerance in this cohort?", INK, 11, mono=False),
        caption(36, card_y + 74, "120 samples · 150 markers", INK3, 9.5),
    ]
    # The second card only exists after the replan — it is the agent's reason.
    finding_y = card_y + 104
    out.append(
        f'<g opacity="0">{keyed("opacity", ["0", "1"], [0.0, SWAP], DUR)}'
        + panel(20, finding_y, 272, 132, stroke=ACCENT, fill="#fffaf3")
        + eyebrow(36, finding_y + 22, "WHAT THE AGENT CHANGED", ACCENT)
        + f'<rect x="204" y="{finding_y + 10}" width="26" height="15" rx="5" fill="{ACCENT_WASH}"/>'
        + f'<text x="217" y="{finding_y + 21}" class="m" font-size="9" font-weight="700" '
          f'fill="{ACCENT}" text-anchor="middle">R2</text>'
        + caption(36, finding_y + 44, "The first scan was not wrong about the", INK2, 11, mono=False)
        + caption(36, finding_y + 60, "arithmetic. It was wrong about the model:", INK2, 11, mono=False)
        + caption(36, finding_y + 76, "ancestry moves the phenotype and most", INK2, 11, mono=False)
        + caption(36, finding_y + 92, "allele frequencies at once.", INK2, 11, mono=False)
        + caption(36, finding_y + 116, "λ 2.8024 → 0.9461 · 8 hits → 3", ACCENT, 9.5)
        + "</g>"
    )

    out.append(revision_group(R1_STEPS, r1_pos, r1_edges, R1_TRACKS, 0.0, SWAP))
    out.append(revision_group(R2_STEPS, r2_pos, r2_edges, R2_TRACKS, SWAP, DUR))

    narration_y = height - 18
    for start, end, colour, text in NARRATION:
        times = [0.0, start, end] if start > 0 else [0.0, end]
        values = ["0", "1", "0"] if start > 0 else ["1", "0"]
        if end >= DUR:
            times, values = times[:-1], values[:-1]
        out.append(
            f'<text x="20" y="{narration_y}" class="m" font-size="10" fill="{INK3}" opacity="0">'
            f'{keyed("opacity", values, times, DUR)}'
            f'<tspan fill="{colour}">▸ </tspan>{esc(text)}</text>'
        )

    out.append("</svg>")
    return "\n".join(out)


# ════════════════════════════════════════════════════════════════════════════
#  Figure 2 — kind decides who may write the step
# ════════════════════════════════════════════════════════════════════════════

def step_kinds() -> str:
    width, height = 820, 322
    out = frame(
        width, height,
        "Which tool may complete which kind of step",
        "The agent completes answer, dynamic and review steps itself through update_step. "
        "Capability and workflow steps are written only by run_capability and run_workflow, "
        "which dispatch to the engine; update_step against those kinds is refused by the "
        "broker.",
    )

    out += [
        panel(24, 108, 150, 76, stroke=ACCENT, fill="#f6f9f7"),
        f'<rect x="24" y="108" width="4" height="76" rx="2" fill="{ACCENT}"/>',
        heading(44, 138, "Agent"),
        caption(44, 156, "any model", INK3, 9.5),
        caption(44, 172, "10 tools", INK3, 9.5),
    ]

    # ── Upper lane: what the agent may close itself ──────────────────────────
    out += [
        panel(296, 34, 312, 96, stroke=ACCENT, fill="#f6f9f7"),
        eyebrow(314, 58, "THE AGENT MAY WRITE THESE", ACCENT),
        caption(314, 118, "work the agent did itself, in its own sandbox", INK3, 9.5),
    ]
    for index, kind in enumerate(("answer", "dynamic", "review")):
        out.append(pill(314 + index * 96, 70, 88, 26, kind, ACCENT, SURFACE, ACCENT))

    out += [
        arrow(174, 130, 288, 86, ACCENT, 1.8, curve=10),
        caption(190, 100, "update_step", ACCENT, 9.5),
        arrow(614, 82, 668, 82, EDGE, 1.6),
        caption(678, 78, "status written", INK3, 9.5),
        caption(678, 92, "by the agent", INK3, 9.5),
    ]

    # ── Lower lane: what only the server may close ───────────────────────────
    out += [
        panel(296, 164, 312, 96, stroke=RUN, fill="#f4f8fc"),
        eyebrow(314, 188, "ONLY THE SERVER WRITES THESE", RUN),
        caption(314, 248, "registered, typed units of work", INK3, 9.5),
    ]
    for index, kind in enumerate(("capability", "workflow")):
        out.append(pill(314 + index * 114, 200, 104, 26, kind, RUN, SURFACE, RUN))

    out += [
        arrow(174, 162, 288, 202, RUN, 1.8, curve=-10),
        caption(184, 196, "run_capability", RUN, 9.5),
        caption(184, 209, "run_workflow", RUN, 9.5),
        arrow(614, 212, 668, 212, EDGE, 1.6),
        caption(678, 208, "status + artifacts", INK3, 9.5),
        caption(678, 222, "written by the engine", INK3, 9.5),
    ]

    # ── The refusal, which is the whole point ────────────────────────────────
    out += [
        arrow(150, 194, 288, 268, ERR, 1.6, dashed=True, curve=-14),
        f'<rect x="150" y="276" width="182" height="22" rx="11" fill="{SURFACE}" '
        f'stroke="{ERR}" stroke-width="1.2"/>',
        f'<text x="241" y="291" class="m" font-size="10" font-weight="600" fill="{ERR}" '
        f'text-anchor="middle">✗ update_step — refused</text>',
        caption(344, 291, "so a capability step reading \"succeeded\" is always a run "
                          "that really happened", INK3, 9.5),
    ]
    out.append("</svg>")
    return "\n".join(out)


# ════════════════════════════════════════════════════════════════════════════
#  Figure 3 — the step transition table, as a machine
# ════════════════════════════════════════════════════════════════════════════

def step_lifecycle() -> str:
    """Explicit coordinates throughout: derived midpoints put labels inside boxes."""
    width, height = 820, 300
    out = frame(
        width, height,
        "The step status transition table",
        "A step goes from pending to running when every dependency has succeeded, or to "
        "skipped when one failed. Running goes to succeeded when the owner writes a result, "
        "or to failed when the runner raises or times out. Failed can return to running via "
        "a bounded retry and skipped via a replan. Succeeded is terminal.",
    )

    w, h = 128, 34
    boxes = {
        "pending":   (90, 120, IDLE, SURFACE),
        "running":   (310, 120, RUN, "#f4f8fc"),
        "succeeded": (560, 64, OK, "#f8faf2"),
        "failed":    (560, 176, ERR, "#fdf6f5"),
        "skipped":   (90, 230, "#b9b0a0", SUNKEN),
    }
    WARN = "#a8864b"

    out += [
        caption(24, 34, "every write goes through this table, so the log can never contain "
                        "a step that went backwards", INK3, 10),
        caption(24, 52, "succeeded is the only terminal state — nothing un-succeeds a step, "
                        "including a replan", OK, 10),
    ]

    # Entry
    out += [
        f'<circle cx="46" cy="137" r="5" fill="{INK3}"/>',
        arrow(56, 137, 84, 137, EDGE, 1.5),
    ]

    # pending → running, label lifted clear of both boxes
    out += [
        arrow(218, 137, 304, 137, RUN, 1.6),
        caption(261, 110, "all deps succeeded", RUN, 9.5, anchor="middle"),
    ]

    # running → succeeded / failed
    out += [
        arrow(438, 130, 554, 84, OK, 1.6, curve=8),
        caption(470, 102, "owner wrote a result", OK, 9.5, anchor="middle"),
        arrow(438, 144, 554, 190, ERR, 1.6, curve=-8),
        caption(468, 192, "raised or timed out", ERR, 9.5, anchor="middle"),
    ]

    # failed → running: routed below both, so it crosses nothing
    out += [
        f'<path d="M600,210 Q600,262 500,262 L432,262 Q378,262 378,164" fill="none" '
        f'stroke="{WARN}" stroke-width="1.5" stroke-linecap="round"/>',
        f'<path d="M373.6,164 L378,156 L382.4,164 Z" fill="{WARN}"/>',
        caption(500, 276, "retry — bounded, with backoff", WARN, 9.5, anchor="middle"),
    ]

    # pending → skipped, and skipped back into flight after a replan
    out += [
        arrow(154, 154, 154, 226, "#b9b0a0", 1.5),
        caption(164, 194, "a dep failed", "#b9b0a0", 9.5),
        arrow(218, 243, 330, 160, ACCENT, 1.5, dashed=True, curve=14),
        caption(238, 224, "a replan unblocked it", ACCENT, 9.5),
    ]

    for name, (x, y, stroke, fill) in boxes.items():
        out.append(panel(x, y, w, h, stroke=stroke, fill=fill, radius=10))
        out.append(
            f'<text x="{x + w // 2}" y="{y + h // 2 + 4}" class="m" font-size="11.5" '
            f'font-weight="600" fill="{INK}" text-anchor="middle">{name}</text>'
        )

    out += [
        arrow(688, 81, 712, 81, EDGE, 1.5),
        f'<circle cx="726" cy="81" r="7" fill="none" stroke="{INK3}" stroke-width="1.5"/>',
        f'<circle cx="726" cy="81" r="3.5" fill="{INK3}"/>',
        caption(740, 85, "terminal", INK3, 9.5),
    ]
    out.append("</svg>")
    return "\n".join(out)


# ════════════════════════════════════════════════════════════════════════════
#  Figure 4 — the four zones of a session
# ════════════════════════════════════════════════════════════════════════════

def session_zones() -> str:
    width, height = 820, 306
    out = frame(
        width, height,
        "The four zones of a session",
        "A session has four directories with different trust. Uploads belong to the user, "
        "artifacts are written by the engine, scratch is the agent's own workspace, and "
        "control holds the plan, the event log and the cursor — no source ref can name it.",
    )

    out.append(caption(24, 34, "one session on disk · every ref re-checked for containment "
                               "and re-verified against its checksum, on every use", INK3, 10))

    zone_x, zone_w = 300, 400
    rows = [
        ("uploads/", "upload:<id>", "the user's files", ACCENT, "#f6f9f7", 58),
        ("artifacts/", "artifact:<id>", "execution output", RUN, "#f4f8fc", 116),
        ("scratch/", "scratch:<path>", "the agent's own workspace", "#6d5bb5", "#f7f5fb", 174),
        ("control/", "— no ref can name it —", "plan · event log · cursor", ERR, "#fdf6f5", 232),
    ]
    out.append(panel(zone_x - 16, 44, zone_w + 32, 234, stroke=HAIRLINE, fill=SUNKEN, radius=14))

    for label, ref, note, stroke, fill, y in rows:
        out += [
            panel(zone_x, y, zone_w, 44, stroke=stroke, fill=fill, radius=10),
            f'<rect x="{zone_x}" y="{y}" width="4" height="44" rx="2" fill="{stroke}"/>',
            f'<text x="{zone_x + 18}" y="{y + 20}" class="m" font-size="11.5" '
            f'font-weight="700" fill="{INK}">{esc(label)}</text>',
            caption(zone_x + 18, y + 35, note, INK3, 9.5),
            f'<text x="{zone_x + zone_w - 18}" y="{y + 27}" class="m" font-size="10" '
            f'fill="{stroke}" text-anchor="end">{esc(ref)}</text>',
        ]

    actors = [
        ("User", 58, ACCENT, "uploads a file", 0),
        ("Engine", 116, RUN, "registers output", 1),
        ("Agent", 174, "#6d5bb5", "reads + writes", 2),
    ]
    for name, y, colour, verb, _ in actors:
        out += [
            panel(24, y - 4, 112, 52, stroke=colour, fill=SURFACE, radius=10),
            f'<text x="80" y="{y + 20}" class="s" font-size="12" font-weight="600" '
            f'fill="{INK}" text-anchor="middle">{esc(name)}</text>',
            caption(80, y + 36, verb, INK3, 9, anchor="middle"),
            arrow(140, y + 22, zone_x - 22, y + 22, colour, 1.6),
        ]

    # The agent also reads the two zones it does not own, and cannot reach control.
    out += [
        arrow(136, 190, zone_x - 22, 76, "#6d5bb5", 1.3, curve=26),
        arrow(136, 186, zone_x - 22, 134, "#6d5bb5", 1.3, curve=14),
        caption(196, 108, "reads", "#6d5bb5", 9),
        arrow(136, 208, zone_x - 22, 250, ERR, 1.5, dashed=True, curve=-14),
        f'<rect x="150" y="264" width="128" height="22" rx="11" fill="{SURFACE}" '
        f'stroke="{ERR}" stroke-width="1.2"/>',
        f'<text x="214" y="279" class="m" font-size="9.5" font-weight="600" fill="{ERR}" '
        f'text-anchor="middle">✗ unreachable</text>',
    ]
    out.append("</svg>")
    return "\n".join(out)


if __name__ == "__main__":
    here = pathlib.Path(__file__).parent
    for name, builder in (
        ("plan-execution", hero),
        ("step-kinds", step_kinds),
        ("step-lifecycle", step_lifecycle),
        ("session-zones", session_zones),
    ):
        target = here / f"{name}.svg"
        target.write_text(builder())
        print(f"wrote {target.name} ({target.stat().st_size:,} bytes)")
