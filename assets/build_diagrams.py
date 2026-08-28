#!/usr/bin/env python3
"""Generate the animated README diagrams from the renderer's own design tokens.

    python assets/build_diagrams.py

The README pictures and the shipped component are the same thing drawn twice,
so the tokens below are copied from ``packages/renderer/src/styles.css`` and
the node geometry from ``PlanGraph.tsx`` — 224x92 cards, a 22px glyph chip, a
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
    "pending":   {"fill": SURFACE,   "stroke": LINE,      "dot": IDLE},
    "running":   {"fill": "#f7fafc", "stroke": "#c4d8ea", "dot": RUN},
    "succeeded": {"fill": "#fbfcfa", "stroke": "#dadece", "dot": OK},
    "failed":    {"fill": "#fdf8f8", "stroke": "#efcbcb", "dot": ERR},
}

GLYPH = {"capability": "◈", "workflow": "⛭", "dynamic": "⌘", "review": "⌕", "answer": "✎"}

# The Chinese status and kind words are the ones the source application used, so
# a reader who knows that UI recognises them here.
STATUS_LABEL = {
    "en": {"pending": "Pending", "running": "Running",
           "succeeded": "Succeeded", "failed": "Failed", "skipped": "Skipped"},
    "zh": {"pending": "等待", "running": "执行中",
           "succeeded": "完成", "failed": "失败", "skipped": "跳过"},
}
KIND_LABEL = {
    "en": {"capability": "Capability", "workflow": "Workflow",
           "dynamic": "Dynamic", "review": "Review", "answer": "Answer"},
    "zh": {"capability": "原子能力", "workflow": "Workflow",
           "dynamic": "动态步骤", "review": "核验", "answer": "回答"},
}

# ── Geometry, from PlanGraph.tsx / layout.ts ────────────────────────────────
NODE_W, NODE_H = 224, 92
LANE_GAP, LAYER_GAP = 26, 58

# Declared once in a <style> block rather than per-element: family names with
# spaces need quotes, and quotes do not survive an XML attribute cleanly.
_SANS = '"Inter", ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto'
_MONO = '"JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas'
_CJK = '"Noto Sans SC", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei"'

FONT_STYLE = {
    "en": f'<style>\n  .s {{ font-family: {_SANS}, sans-serif; }}\n'
          f'  .m {{ font-family: {_MONO}, monospace; }}\n</style>',
    # CJK glyphs come from the system face; the Latin faces still win for the
    # capability ids and numbers, which is what the mono class is mostly for.
    "zh": f'<style>\n  .s {{ font-family: {_SANS}, {_CJK}, sans-serif; }}\n'
          f'  .m {{ font-family: {_MONO}, {_CJK}, monospace; }}\n</style>',
}


def esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def keyed(attr: str, values: list[str], times: list[float], dur: float) -> str:
    """A discrete SMIL track. Status changes are steps, not fades."""
    return (
        f'<animate attributeName="{attr}" dur="{dur}s" repeatCount="indefinite" '
        f'calcMode="discrete" values="{";".join(values)}" '
        f'keyTimes="{";".join(f"{t / dur:.4f}" for t in times)}"/>'
    )


def node_svg(x: int, y: int, step: dict, track: list[tuple[float, str]], dur: float,
             lang: str = "en") -> str:
    """One plan node, animated across its status track."""
    times = [t for t, _ in track]
    states = [s for _, s in track]
    glyph = GLYPH.get(step["kind"], "◈")
    meta = step.get("capability") or KIND_LABEL[lang][step["kind"]]

    fills = [STATUS[s]["fill"] for s in states]
    strokes = [STATUS[s]["stroke"] for s in states]
    dots = [STATUS[s]["dot"] for s in states]
    labels = [STATUS_LABEL[lang][s] for s in states]

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
        f'fill="{INK}">{esc(step["title"][lang] if isinstance(step["title"], dict) else step["title"])}</text>',
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
    # The step id follows the status word. Chinese labels are much shorter than
    # English ones, so a fixed offset would leave a hole in one language or
    # collide in the other.
    id_x = x + 26 + max(len(label) * (10 if lang == "zh" else 5.9) for label in labels) + 12
    parts.append(
        f'<text x="{id_x:.0f}" y="{y + 80}" class="m" font-size="10" fill="{INK3}">'
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




# ── Copy, in both languages ────────────────────────────────────────────────
#
# Every user-visible string in the figures below. The Chinese status and kind
# words match the source application's own UI vocabulary.
TEXT = {
    "en": {
        "hero.title": "An association plan replacing itself",
        "hero.aria": "Revision 1 of an association plan runs to a review step, which reads a genomic inflation factor of 2.80 off the artifact and concludes the model is confounded. Revision 2 replaces it with a structure-aware plan in which ancestry axes and a relatedness matrix are dispatched together, and the corrected scan returns a calibrated inflation factor of 0.95.",
        "hero.goal": "Find markers associated with salt tolerance in the uploaded cohort.",
        "hero.steps5": "5 steps",
        "hero.steps9": "9 steps",
        "hero.summary": "QC the genotypes, scan every marker, correct for multiple testing.",
        "hero.replanned": "Replanned: ",
        "hero.reason": "λ = 2.8024 in revision 1 — the statistics are inflated genome-wide, which is population structure rather than signal.",
        "hero.card1.eyebrow": "WHAT THE USER ASKED",
        "hero.card1.l1": "Which markers are associated with salt",
        "hero.card1.l2": "tolerance in this cohort?",
        "hero.card1.meta": "120 samples · 150 markers",
        "hero.card2.eyebrow": "WHAT THE AGENT CHANGED",
        "hero.card2.l1": "The first scan was not wrong about the",
        "hero.card2.l2": "arithmetic. It was wrong about the model:",
        "hero.card2.l3": "ancestry moves the phenotype and most",
        "hero.card2.l4": "allele frequencies at once.",
        "hero.card2.meta": "λ 2.8024 → 0.9461 · 8 hits → 3",
        "step.qc": "Quality control",
        "step.assoc1": "Association scan",
        "step.correct": "Multiple testing",
        "step.review": "Is the model calibrated?",
        "step.answer": "Report the loci",
        "step.pca": "Ancestry axes",
        "step.kinship": "Relatedness matrix",
        "step.assoc2": "Structure-aware scan",
        "step.annotate": "Annotate the hits",
        "n.1": "revision 1 published · 5 steps · validated before anything ran",
        "n.2": "quality control · 150 markers in, 148 out",
        "n.3": "one scan, one marker at a time — nothing to run in parallel",
        "n.4": "correcting for multiple testing · 8 markers survive",
        "n.5": "review: reading the genomic inflation factor off the artifact",
        "n.6": "λ = 2.8024 — every statistic is inflated, not eight loci. 5 of the 8 are ancestry.",
        "n.7": "revision 2 published · the review step rewrote the plan, not the answer",
        "n.8": "ancestry and relatedness dispatched together — no edge between them",
        "n.9": "the scan waited for both parents · fan-in, not a race",
        "n.10": "correcting for multiple testing",
        "n.11": "λ = 0.9461 · 3 markers survive, and all three are real",
        "k.title": "Which tool may complete which kind of step",
        "k.aria": "The agent completes answer, dynamic and review steps itself through update_step. Capability and workflow steps are written only by run_capability and run_workflow, which dispatch to the engine; update_step against those kinds is refused by the broker.",
        "k.agent": "Agent",
        "k.agent.l1": "any model",
        "k.agent.l2": "10 tools",
        "k.self": "THE AGENT MAY WRITE THESE",
        "k.self.note": "work the agent did itself, in its own sandbox",
        "k.serv": "ONLY THE SERVER WRITES THESE",
        "k.serv.note": "registered, typed units of work",
        "k.out1a": "status written",
        "k.out1b": "by the agent",
        "k.out2a": "status + artifacts",
        "k.out2b": "written by the engine",
        "k.refused": "✗ update_step — refused",
        "k.point": "so a capability step reading \"succeeded\" is always a run that really happened",
        "l.title": "The step status transition table",
        "l.aria": "A step goes from pending to running when every dependency has succeeded, or to skipped when one failed. Running goes to succeeded when the owner writes a result, or to failed when the runner raises or times out. Failed can return to running via a bounded retry and skipped via a replan. Succeeded is terminal.",
        "l.head1": "every write goes through this table, so the log can never contain a step that went backwards",
        "l.head2": "succeeded is the only terminal state — nothing un-succeeds a step, including a replan",
        "l.deps": "all deps succeeded",
        "l.owner": "owner wrote a result",
        "l.raised": "raised or timed out",
        "l.retry": "retry — bounded, with backoff",
        "l.depfail": "a dep failed",
        "l.replan": "a replan unblocked it",
        "l.terminal": "terminal",
        "z.title": "The four zones of a session",
        "z.aria": "A session has four directories with different trust. Uploads belong to the user, artifacts are written by the engine, scratch is the agent's own workspace, and control holds the plan, the event log and the cursor — no source ref can name it.",
        "z.head": "one session on disk · every ref re-checked for containment and re-verified against its checksum, on every use",
        "z.uploads": "the user's files",
        "z.artifacts": "execution output",
        "z.scratch": "the agent's own workspace",
        "z.control": "plan · event log · cursor",
        "z.noref": "— no ref can name it —",
        "z.user": "User",
        "z.user.v": "uploads a file",
        "z.engine": "Engine",
        "z.engine.v": "registers output",
        "z.agentn": "Agent",
        "z.agent.v": "reads + writes",
        "z.reads": "reads",
        "z.unreachable": "✗ unreachable",
    },
    "zh": {
        "hero.title": "一个把自己推翻重来的关联分析计划",
        "hero.aria": "计划的第 1 版跑到核验步骤，从产物里读出基因组膨胀因子 2.80，判定模型被混杂。第 2 版把它换成考虑群体结构的计划：祖先主成分和亲缘矩阵之间没有依赖边，被同时派发；校正后的扫描给出 0.95 的膨胀因子。",
        "hero.goal": "在上传的群体中找出与耐盐性关联的位点。",
        "hero.steps5": "5 个步骤",
        "hero.steps9": "9 个步骤",
        "hero.summary": "基因型质控，逐个位点扫描，再做多重检验校正。",
        "hero.replanned": "已重新规划：",
        "hero.reason": "第 1 版 λ = 2.8024 —— 统计量在全基因组范围被抬高，这是群体结构，不是信号。",
        "hero.card1.eyebrow": "用户原话",
        "hero.card1.l1": "这个群体里，哪些位点跟耐盐性",
        "hero.card1.l2": "有关联？",
        "hero.card1.meta": "120 个样本 · 150 个位点",
        "hero.card2.eyebrow": "智能体改了什么",
        "hero.card2.l1": "第一次扫描的算术没有错，",
        "hero.card2.l2": "错的是模型：祖先来源同时",
        "hero.card2.l3": "牵动了表型和大部分位点的",
        "hero.card2.l4": "等位基因频率。",
        "hero.card2.meta": "λ 2.8024 → 0.9461 · 命中 8 → 3",
        "step.qc": "基因型质控",
        "step.assoc1": "关联扫描",
        "step.correct": "多重检验校正",
        "step.review": "模型是否校准？",
        "step.answer": "汇报关联位点",
        "step.pca": "祖先主成分",
        "step.kinship": "亲缘关系矩阵",
        "step.assoc2": "考虑结构的扫描",
        "step.annotate": "注释命中位点",
        "n.1": "第 1 版已发布 · 5 个步骤 · 在任何东西开跑之前就已校验",
        "n.2": "质控 · 进 150 个位点，出 148 个",
        "n.3": "一次扫描，逐个位点 —— 没有任何可以并行的东西",
        "n.4": "多重检验校正 · 8 个位点存活",
        "n.5": "核验：从产物里读出基因组膨胀因子",
        "n.6": "λ = 2.8024 —— 被抬高的是每一个统计量，不是那 8 个位点。8 个里有 5 个是祖先来源。",
        "n.7": "第 2 版已发布 · 核验步骤改写的是计划，不是答案",
        "n.8": "祖先主成分和亲缘矩阵被同时派发 —— 它们之间没有依赖边",
        "n.9": "扫描等齐了两个父节点 · 是汇聚，不是竞争",
        "n.10": "多重检验校正",
        "n.11": "λ = 0.9461 · 3 个位点存活，而且三个都是真的",
        "k.title": "哪个工具可以完成哪一类步骤",
        "k.aria": "answer、dynamic、review 三类步骤由智能体自己通过 update_step 写入。capability 和 workflow 只能由 run_capability / run_workflow 写入，它们派发给引擎；对这两类调用 update_step 会被 broker 拒绝。",
        "k.agent": "智能体",
        "k.agent.l1": "任意模型",
        "k.agent.l2": "10 个工具",
        "k.self": "智能体可以自己写这几类",
        "k.self.note": "智能体在自己沙箱里亲手做的工作",
        "k.serv": "这两类只有服务端能写",
        "k.serv.note": "已注册的、带类型契约的工作单元",
        "k.out1a": "状态由智能体",
        "k.out1b": "自己写入",
        "k.out2a": "状态和产物",
        "k.out2b": "由引擎写入",
        "k.refused": "✗ update_step —— 被拒绝",
        "k.point": "所以 capability 步骤显示「完成」，就一定对应一次真实发生过的执行",
        "l.title": "步骤状态的转移表",
        "l.aria": "依赖全部成功时，步骤从 pending 进入 running；有依赖失败则进入 skipped。running 在归属者写下结果时进入 succeeded，在 runner 抛错或超时时进入 failed。failed 可以通过有上限的重试回到 running，skipped 可以通过重新规划回到 running。succeeded 是终态。",
        "l.head1": "每一次写入都要过这张表，所以日志里不可能出现倒退的步骤",
        "l.head2": "succeeded 是唯一的终态 —— 没有任何东西能让它退回去，重新规划也不行",
        "l.deps": "依赖全部成功",
        "l.owner": "归属者写下了结果",
        "l.raised": "抛错或超时",
        "l.retry": "重试 —— 有次数上限，带退避",
        "l.depfail": "有依赖失败了",
        "l.replan": "重新规划把它解锁了",
        "l.terminal": "终态",
        "z.title": "会话的四个信任区",
        "z.aria": "一个会话有四个信任级别不同的目录。uploads 属于用户，artifacts 由引擎写入，scratch 是智能体自己的工作区，control 存放计划、事件日志和游标 —— 任何 source ref 都指不到它。",
        "z.head": "磁盘上的一个会话 · 每次使用都会重新校验路径是否越界，并重新比对校验和",
        "z.uploads": "用户的文件",
        "z.artifacts": "执行产生的产物",
        "z.scratch": "智能体自己的工作区",
        "z.control": "计划 · 事件日志 · 游标",
        "z.noref": "—— 任何 ref 都指不到 ——",
        "z.user": "用户",
        "z.user.v": "上传文件",
        "z.engine": "引擎",
        "z.engine.v": "登记产物",
        "z.agentn": "智能体",
        "z.agent.v": "可读可写",
        "z.reads": "只读",
        "z.unreachable": "✗ 触及不到",
    },
}


# ── Shared chrome ───────────────────────────────────────────────────────────

def frame(width: int, height: int, title: str, aria: str, lang: str = "en") -> list[str]:
    """Page, border and font declarations — every figure opens with these."""
    return [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="{esc(aria)}">',
        f"<title>{esc(title)}</title>",
        FONT_STYLE[lang],
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
    "qc":      {"id": "qc", "title": {k: TEXT[k]["step.qc"] for k in TEXT}, "kind": "capability", "capability": "gwas.qc"},
    "assoc":   {"id": "assoc", "title": {k: TEXT[k]["step.assoc1"] for k in TEXT}, "kind": "capability", "capability": "gwas.associate"},
    "correct": {"id": "correct", "title": {k: TEXT[k]["step.correct"] for k in TEXT}, "kind": "capability", "capability": "gwas.correct"},
    "review":  {"id": "review", "title": {k: TEXT[k]["step.review"] for k in TEXT}, "kind": "review", "capability": None},
    "answer":  {"id": "answer", "title": {k: TEXT[k]["step.answer"] for k in TEXT}, "kind": "answer", "capability": None},
}

R2_STEPS = {
    "qc":       {"id": "qc", "title": {k: TEXT[k]["step.qc"] for k in TEXT}, "kind": "capability", "capability": "gwas.qc"},
    "pca":      {"id": "pca", "title": {k: TEXT[k]["step.pca"] for k in TEXT}, "kind": "capability", "capability": "gwas.pca"},
    "kinship":  {"id": "kinship", "title": {k: TEXT[k]["step.kinship"] for k in TEXT}, "kind": "capability", "capability": "gwas.kinship"},
    "assoc":    {"id": "assoc", "title": {k: TEXT[k]["step.assoc2"] for k in TEXT}, "kind": "capability", "capability": "gwas.associate"},
    "correct":  {"id": "correct", "title": {k: TEXT[k]["step.correct"] for k in TEXT}, "kind": "capability", "capability": "gwas.correct"},
    "annotate": {"id": "annotate", "title": {k: TEXT[k]["step.annotate"] for k in TEXT}, "kind": "capability", "capability": "gwas.annotate"},
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

def narration(t: dict) -> list[tuple[float, float, str, str]]:
    """Windows are shared across languages; only the words change."""
    return [
        (0.0, 1.0, ACCENT, t["n.1"]),
        (1.0, 2.9, RUN, t["n.2"]),
        (2.9, 5.1, RUN, t["n.3"]),
        (5.1, 6.9, RUN, t["n.4"]),
        (6.9, 8.2, RUN, t["n.5"]),
        (8.2, SWAP, ERR, t["n.6"]),
        (SWAP, SWAP + 1.7, ACCENT, t["n.7"]),
        (SWAP + 1.7, SWAP + 4.4, RUN, t["n.8"]),
        (SWAP + 4.4, SWAP + 6.3, RUN, t["n.9"]),
        (SWAP + 6.3, SWAP + 7.3, RUN, t["n.10"]),
        (SWAP + 7.3, DUR, OK, t["n.11"]),
    ]


def hero(lang: str = "en") -> str:
    t = TEXT[lang]
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
        body += [node_svg(*pos[key], steps[key], tracks[key], DUR, lang) for key in steps]

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
        t["hero.title"],
        t["hero.aria"],
        lang,
    )

    # Header chrome
    out += [
        f'<rect x="1" y="1" width="{width - 2}" height="{header_h}" fill="{SURFACE}"/>',
        f'<line x1="0" y1="{header_h}" x2="{width}" y2="{header_h}" stroke="{HAIRLINE}"/>',
        heading(20, 48, t["hero.goal"]),
    ]
    # Badge + step count flip at the swap.
    for label, count, start, end in (("R1", t["hero.steps5"], 0.0, SWAP), ("R2", t["hero.steps9"], SWAP, DUR)):
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
        f'{esc(t["hero.summary"])}</text>',
        f'<text x="20" y="{header_h + 23}" class="s" font-size="11" fill="{INK2}" opacity="0">'
        f'{keyed("opacity", ["0", "1"], [0.0, SWAP], DUR)}'
        f'<tspan font-weight="600" fill="{ACCENT}">{esc(t["hero.replanned"])}</tspan>'
        f'{esc(t["hero.reason"])}</text>',
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
        eyebrow(36, card_y + 22, t["hero.card1.eyebrow"], INK3),
        caption(36, card_y + 42, t["hero.card1.l1"], INK, 11, mono=False),
        caption(36, card_y + 58, t["hero.card1.l2"], INK, 11, mono=False),
        caption(36, card_y + 74, t["hero.card1.meta"], INK3, 9.5),
    ]
    # The second card only exists after the replan — it is the agent's reason.
    finding_y = card_y + 104
    out.append(
        f'<g opacity="0">{keyed("opacity", ["0", "1"], [0.0, SWAP], DUR)}'
        + panel(20, finding_y, 272, 132, stroke=ACCENT, fill="#fffaf3")
        + eyebrow(36, finding_y + 22, t["hero.card2.eyebrow"], ACCENT)
        + f'<rect x="204" y="{finding_y + 10}" width="26" height="15" rx="5" fill="{ACCENT_WASH}"/>'
        + f'<text x="217" y="{finding_y + 21}" class="m" font-size="9" font-weight="700" '
          f'fill="{ACCENT}" text-anchor="middle">R2</text>'
        + caption(36, finding_y + 44, t["hero.card2.l1"], INK2, 11, mono=False)
        + caption(36, finding_y + 60, t["hero.card2.l2"], INK2, 11, mono=False)
        + caption(36, finding_y + 76, t["hero.card2.l3"], INK2, 11, mono=False)
        + caption(36, finding_y + 92, t["hero.card2.l4"], INK2, 11, mono=False)
        + caption(36, finding_y + 116, t["hero.card2.meta"], ACCENT, 9.5)
        + "</g>"
    )

    out.append(revision_group(R1_STEPS, r1_pos, r1_edges, R1_TRACKS, 0.0, SWAP))
    out.append(revision_group(R2_STEPS, r2_pos, r2_edges, R2_TRACKS, SWAP, DUR))

    narration_y = height - 18
    for start, end, colour, text in narration(t):
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
#  Figure 2 — the workbench tour: one parent, three parallel branches
# ════════════════════════════════════════════════════════════════════════════

def workbench(lang: str = "en") -> str:
    """A first-glance workbench view, shaped like the product rather than a chart.

    The original hero is deliberately a story about replanning.  This figure is
    the complementary opening shot: the reader can see the agent's request on
    the left and the execution graph on the right, with the independent branch
    layer already in flight.  It uses the same card geometry and colour tokens
    as :func:`hero`, so the README never invents a second visual language.
    """

    copy = {
        "en": {
            "title": "The workbench: one plan, three branches in parallel",
            "aria": (
                "A LoomCraft workbench. A user asks for a report; the agent publishes a "
                "eleven-step plan. Variant normalisation is complete, three independent "
                "analysis branches are visible, and each branch continues to a QC step "
                "before the final report. The three branches run concurrently because "
                "there is no edge between them."
            ),
            "header": "Agent-authored plan · live execution",
            "published": "PUBLISHED",
            "parallel": "3 branches running",
            "steps": "11 steps",
            "user_eyebrow": "USER REQUEST",
            "user_l1": "Give me a markdown report",
            "user_l2": "from this trial dataset.",
            "user_meta": "one upload · one session",
            "agent_eyebrow": "AGENT PLAN",
            "agent_l1": "publish_plan · revision 1",
            "agent_l2": "normalize → [ PCA | phenotype | kinship ]",
            "agent_l3": "three branches → QC → report",
            "point_eyebrow": "WHY THIS MATTERS",
            "point_l1": "No parallel keyword.",
            "point_l2": "The dependency graph is the",
            "point_l3": "scheduler's concurrency plan.",
            "point_meta": "event stream is the source of truth",
            "branch": "NO EDGE BETWEEN THESE · DISPATCH TOGETHER",
            "footer": "The graph is the plan. The event log is the receipt.",
            "normalize": "Normalize variants",
            "pca": "Population structure",
            "phenotype": "Prepare phenotype",
            "kinship": "Relatedness matrix",
            "scan_yield": "GWAS · yield",
            "scan_depth": "GWAS · depth",
            "scan_height": "GWAS · height",
            "qc_yield": "QC · yield",
            "qc_depth": "QC · depth",
            "qc_height": "QC · height",
            "report": "Compose report",
        },
        "zh": {
            "title": "工作台：一张计划，三条分支同时运行",
            "aria": (
                "LoomCraft 工作台：用户提出报告需求，智能体发布一张十一步计划。变异标准化已完成，"
                "三条互不依赖的分析分支正在运行；每条分支随后进入核验，最后汇聚成报告。"
                "因为分支之间没有依赖边，所以它们会并发执行。"
            ),
            "header": "智能体发布的计划 · 实时执行",
            "published": "已发布",
            "parallel": "3 条分支执行中",
            "steps": "11 个步骤",
            "user_eyebrow": "用户原话",
            "user_l1": "给我一份 markdown 报告",
            "user_l2": "总结这份试验数据。",
            "user_meta": "一次上传 · 一个会话",
            "agent_eyebrow": "智能体计划",
            "agent_l1": "publish_plan · 第 1 版",
            "agent_l2": "标准化 → [ PCA | 表型 | 亲缘 ]",
            "agent_l3": "三条分支 → 核验 → 汇总报告",
            "point_eyebrow": "关键点",
            "point_l1": "不需要 parallel 关键字。",
            "point_l2": "依赖图本身就是调度器的",
            "point_l3": "并发计划。",
            "point_meta": "事件流才是唯一事实来源",
            "branch": "三条分支互不依赖 · 同时派发",
            "footer": "图是计划，事件日志是凭证。",
            "normalize": "变异标准化",
            "pca": "群体结构 PCA",
            "phenotype": "表型准备",
            "kinship": "亲缘关系矩阵",
            "scan_yield": "GWAS · 背膘厚",
            "scan_depth": "GWAS · 眼肌深度",
            "scan_height": "GWAS · 体长",
            "qc_yield": "结果核验 · 背膘厚",
            "qc_depth": "结果核验 · 眼肌深度",
            "qc_height": "结果核验 · 体长",
            "report": "汇总报告",
        },
    }
    t = copy[lang]
    width, height = 1100, 720
    out = frame(width, height, t["title"], t["aria"], lang)

    # Workbench shell and dot-grid canvas.
    out += [
        f'<rect x="1" y="1" width="{width - 2}" height="64" fill="{SURFACE}"/>',
        f'<line x1="0" y1="64" x2="{width}" y2="64" stroke="{HAIRLINE}"/>',
        f'<text x="28" y="39" class="s" font-size="15" font-weight="650" fill="{INK}">'
        f'{esc(t["header"])}</text>',
        f'<rect x="690" y="19" width="96" height="24" rx="8" fill="{ACCENT_WASH}"/>',
        f'<text x="738" y="35" class="m" font-size="9.5" font-weight="700" fill="{ACCENT}" '
        f'text-anchor="middle">{esc(t["published"])}</text>',
        f'<rect x="798" y="19" width="142" height="24" rx="8" fill="#e9f0f9" stroke="#c4d8ea"/>',
        f'<circle cx="814" cy="31" r="3.5" fill="{RUN}"/>',
        f'<text x="824" y="35" class="s" font-size="10" font-weight="600" fill="{RUN}">'
        f'{esc(t["parallel"])}</text>',
        f'<rect x="952" y="19" width="116" height="24" rx="8" fill="{SUNKEN}" stroke="{HAIRLINE}"/>',
        f'<text x="1010" y="35" class="m" font-size="10" font-weight="600" fill="{INK3}" '
        f'text-anchor="middle">{esc(t["steps"])}</text>',
        f'<defs><pattern id="workbench-dots" width="20" height="20" patternUnits="userSpaceOnUse">'
        f'<circle cx="1" cy="1" r="1" fill="{GRAPH_DOT}"/></pattern></defs>',
        f'<rect x="1" y="65" width="{width - 2}" height="{height - 66}" fill="{GRAPH_CANVAS}"/>',
        f'<rect x="1" y="65" width="{width - 2}" height="{height - 66}" fill="url(#workbench-dots)"/>',
        f'<line x1="318" y1="65" x2="318" y2="{height}" stroke="{HAIRLINE}"/>',
    ]

    # Left-hand conversation rail, deliberately close to the product UI.
    out += [
        panel(24, 92, 266, 84),
        eyebrow(40, 114, t["user_eyebrow"], INK3),
        caption(40, 137, t["user_l1"], INK, 12, mono=False),
        caption(40, 155, t["user_l2"], INK, 12, mono=False),
        caption(40, 168, t["user_meta"], INK3, 9.5),
        panel(24, 196, 266, 112, stroke=ACCENT, fill="#f7faf8"),
        f'<rect x="24" y="196" width="4" height="112" rx="2" fill="{ACCENT}"/>',
        eyebrow(40, 218, t["agent_eyebrow"], ACCENT),
        caption(40, 243, t["agent_l1"], INK, 11, mono=False),
        caption(40, 264, t["agent_l2"], INK2, 10, mono=True),
        caption(40, 284, t["agent_l3"], INK2, 10, mono=True),
        panel(24, 332, 266, 142, stroke=RUN, fill="#f4f8fc"),
        f'<rect x="24" y="332" width="4" height="142" rx="2" fill="{RUN}"/>',
        eyebrow(40, 356, t["point_eyebrow"], RUN),
        f'<text x="40" y="383" class="s" font-size="13" font-weight="650" fill="{INK}">'
        f'{esc(t["point_l1"])}</text>',
        caption(40, 407, t["point_l2"], INK2, 11, mono=False),
        caption(40, 425, t["point_l3"], INK2, 11, mono=False),
        caption(40, 454, t["point_meta"], RUN, 9.5),
        # A small legend is useful in a screenshot and costs less than another paragraph.
        eyebrow(40, 536, "STATUS" if lang == "en" else "状态", INK3),
        f'<circle cx="44" cy="558" r="3.5" fill="{OK}"/>',
        caption(54, 562, "succeeded" if lang == "en" else "完成", INK3, 10, mono=False),
        f'<circle cx="44" cy="580" r="3.5" fill="{RUN}"/>',
        caption(54, 584, "running" if lang == "en" else "执行中", INK3, 10, mono=False),
        f'<circle cx="44" cy="602" r="3.5" fill="{IDLE}"/>',
        caption(54, 606, "pending" if lang == "en" else "等待", INK3, 10, mono=False),
    ]

    # The graph is intentionally a clean fan-out/fan-in shape.  The cards use
    # node_svg, so their geometry and status vocabulary cannot drift from the UI.
    def localized(key: str) -> dict[str, str]:
        return {language: copy[language][key] for language in copy}

    def step(step_id: str, title_key: str, capability: str) -> dict[str, object]:
        return {
            "id": step_id,
            "title": localized(title_key),
            "kind": "capability",
            "capability": capability,
        }

    steps = {
        "normalize": step("normalize", "normalize", "genotype.variant_normalize"),
        "pca": step("pca", "pca", "genotype.plink_pca"),
        "phenotype": step("phenotype", "phenotype", "phenotype.prepare"),
        "kinship": step("kinship", "kinship", "genotype.kinship"),
        "scan_yield": step("scan_yield", "scan_yield", "gwas.scan_yield"),
        "scan_depth": step("scan_depth", "scan_depth", "gwas.scan_depth"),
        "scan_height": step("scan_height", "scan_height", "gwas.scan_height"),
        "qc_yield": step("qc_yield", "qc_yield", "gwas.qc_yield"),
        "qc_depth": step("qc_depth", "qc_depth", "gwas.qc_depth"),
        "qc_height": step("qc_height", "qc_height", "gwas.qc_height"),
    }
    # A report card is drawn separately so the branch count remains obvious;
    # the executable example adds the same fan-in as an answer step.
    positions = {
        "normalize": (598, 92),
        "pca": (350, 220),
        "phenotype": (598, 220),
        "kinship": (846, 220),
        "scan_yield": (350, 350),
        "scan_depth": (598, 350),
        "scan_height": (846, 350),
        "qc_yield": (350, 480),
        "qc_depth": (598, 480),
        "qc_height": (846, 480),
    }
    tracks = {
        "normalize": [(0, "succeeded")],
        "pca": [(0, "running"), (2.8, "succeeded")],
        "phenotype": [(0, "running"), (2.8, "succeeded")],
        "kinship": [(0, "running"), (2.8, "succeeded")],
        "scan_yield": [(0, "pending"), (3.1, "running"), (6.2, "succeeded")],
        "scan_depth": [(0, "pending"), (3.1, "running"), (6.2, "succeeded")],
        "scan_height": [(0, "pending"), (3.1, "running"), (6.2, "succeeded")],
        "qc_yield": [(0, "pending"), (6.5, "running"), (8.6, "succeeded")],
        "qc_depth": [(0, "pending"), (6.5, "running"), (8.6, "succeeded")],
        "qc_height": [(0, "pending"), (6.5, "running"), (8.6, "succeeded")],
    }
    report = {
        "id": "report",
        "title": localized("report"),
        "kind": "answer",
        "capability": None,
    }
    positions["report"] = (598, 610)
    tracks["report"] = [(0, "pending"), (8.9, "running"), (11.0, "succeeded")]
    steps["report"] = report

    edges = [
        ("normalize", "pca"), ("normalize", "phenotype"), ("normalize", "kinship"),
        ("pca", "scan_yield"),
        ("phenotype", "scan_depth"),
        ("kinship", "scan_height"),
        ("scan_yield", "qc_yield"), ("scan_depth", "qc_depth"), ("scan_height", "qc_height"),
        ("qc_yield", "report"), ("qc_depth", "report"), ("qc_height", "report"),
    ]

    def node_bottom(node_id: str) -> tuple[int, int]:
        x, y = positions[node_id]
        return x + NODE_W // 2, y + NODE_H

    def node_top(node_id: str) -> tuple[int, int]:
        x, y = positions[node_id]
        return x + NODE_W // 2, y

    def tones(node_id: str) -> list[tuple[float, str]]:
        track = tracks[node_id]
        running = next((time for time, state in track if state == "running"), None)
        succeeded = next((time for time, state in track if state == "succeeded"), None)
        if running is None:
            return [(0, "done" if succeeded is not None else "idle")]
        result: list[tuple[float, str]] = [(0, "active" if running == 0 else "idle")]
        if running > 0:
            result.append((running, "active"))
        if succeeded is not None:
            result.append((succeeded, "done"))
        return result

    # Draw edges before cards, so every connector tucks underneath its target.
    for parent, child in edges:
        out.append(edge_svg(node_bottom(parent), node_top(child), tones(child), 12.0))
    out.append(
        f'<rect x="410" y="185" width="600" height="24" rx="12" fill="#eef4fb" '
        f'stroke="#b8cde4" stroke-width="1"/>'
    )
    out.append(
        f'<text x="710" y="201" class="m" font-size="9.5" font-weight="700" fill="{RUN}" '
        f'text-anchor="middle">{esc(t["branch"])}</text>'
    )
    for node_id, node in steps.items():
        out.append(node_svg(*positions[node_id], node, tracks[node_id], 12.0, lang))

    # Point at the fan-out without adding another competing colour or icon.
    out += [
        f'<path d="M290,250 C330,250 330,250 340,250" fill="none" stroke="{ACCENT}" '
        f'stroke-width="1.4" stroke-dasharray="4 4"/>',
        f'<path d="M332,246 L340,250 L332,254 Z" fill="{ACCENT}"/>',
        caption(204, 240, "publish → run" if lang == "en" else "发布 → 执行", ACCENT, 9.5),
        f'<text x="28" y="712" class="m" font-size="10" fill="{INK3}">'
        f'<tspan fill="{ACCENT}">▸ </tspan>{esc(t["footer"])}</text>',
    ]
    out.append("</svg>")
    return "\n".join(out)


# ════════════════════════════════════════════════════════════════════════════
#  Figure 3 — kind decides who may write the step
# ════════════════════════════════════════════════════════════════════════════

def step_kinds(lang: str = "en") -> str:
    t = TEXT[lang]
    width, height = 820, 322
    out = frame(
        width, height,
        t["k.title"],
        t["k.aria"],
        lang,
    )

    out += [
        panel(24, 108, 150, 76, stroke=ACCENT, fill="#f6f9f7"),
        f'<rect x="24" y="108" width="4" height="76" rx="2" fill="{ACCENT}"/>',
        heading(44, 138, t["k.agent"]),
        caption(44, 156, t["k.agent.l1"], INK3, 9.5),
        caption(44, 172, t["k.agent.l2"], INK3, 9.5),
    ]

    # ── Upper lane: what the agent may close itself ──────────────────────────
    out += [
        panel(296, 34, 312, 96, stroke=ACCENT, fill="#f6f9f7"),
        eyebrow(314, 58, t["k.self"], ACCENT),
        caption(314, 118, t["k.self.note"], INK3, 9.5),
    ]
    for index, kind in enumerate(("answer", "dynamic", "review")):
        out.append(pill(314 + index * 96, 70, 88, 26, kind, ACCENT, SURFACE, ACCENT))

    out += [
        arrow(174, 130, 288, 86, ACCENT, 1.8, curve=10),
        caption(190, 100, "update_step", ACCENT, 9.5),
        arrow(614, 82, 668, 82, EDGE, 1.6),
        caption(678, 78, t["k.out1a"], INK3, 9.5),
        caption(678, 92, t["k.out1b"], INK3, 9.5),
    ]

    # ── Lower lane: what only the server may close ───────────────────────────
    out += [
        panel(296, 164, 312, 96, stroke=RUN, fill="#f4f8fc"),
        eyebrow(314, 188, t["k.serv"], RUN),
        caption(314, 248, t["k.serv.note"], INK3, 9.5),
    ]
    for index, kind in enumerate(("capability", "workflow")):
        out.append(pill(314 + index * 114, 200, 104, 26, kind, RUN, SURFACE, RUN))

    out += [
        arrow(174, 162, 288, 202, RUN, 1.8, curve=-10),
        caption(184, 196, "run_capability", RUN, 9.5),
        caption(184, 209, "run_workflow", RUN, 9.5),
        arrow(614, 212, 668, 212, EDGE, 1.6),
        caption(678, 208, t["k.out2a"], INK3, 9.5),
        caption(678, 222, t["k.out2b"], INK3, 9.5),
    ]

    # ── The refusal, which is the whole point ────────────────────────────────
    out += [
        arrow(150, 194, 288, 268, ERR, 1.6, dashed=True, curve=-14),
        f'<rect x="150" y="276" width="182" height="22" rx="11" fill="{SURFACE}" '
        f'stroke="{ERR}" stroke-width="1.2"/>',
        f'<text x="241" y="291" class="m" font-size="10" font-weight="600" fill="{ERR}" '
        f'text-anchor="middle">✗ update_step — refused</text>',
        caption(344, 291, t["k.point"], INK3, 9.5),
    ]
    out.append("</svg>")
    return "\n".join(out)


# ════════════════════════════════════════════════════════════════════════════
#  Figure 4 — the step transition table, as a machine
# ════════════════════════════════════════════════════════════════════════════

def step_lifecycle(lang: str = "en") -> str:
    """Explicit coordinates throughout: derived midpoints put labels inside boxes."""
    t = TEXT[lang]
    width, height = 820, 300
    out = frame(
        width, height,
        t["l.title"],
        t["l.aria"],
        lang,
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
        caption(24, 34, t["l.head1"], INK3, 10),
        caption(24, 52, t["l.head2"], OK, 10),
    ]

    # Entry
    out += [
        f'<circle cx="46" cy="137" r="5" fill="{INK3}"/>',
        arrow(56, 137, 84, 137, EDGE, 1.5),
    ]

    # pending → running, label lifted clear of both boxes
    out += [
        arrow(218, 137, 304, 137, RUN, 1.6),
        caption(261, 110, t["l.deps"], RUN, 9.5, anchor="middle"),
    ]

    # running → succeeded / failed
    out += [
        arrow(438, 130, 554, 84, OK, 1.6, curve=8),
        caption(470, 102, t["l.owner"], OK, 9.5, anchor="middle"),
        arrow(438, 144, 554, 190, ERR, 1.6, curve=-8),
        caption(468, 192, t["l.raised"], ERR, 9.5, anchor="middle"),
    ]

    # failed → running: routed below both, so it crosses nothing
    out += [
        f'<path d="M600,210 Q600,262 500,262 L432,262 Q378,262 378,164" fill="none" '
        f'stroke="{WARN}" stroke-width="1.5" stroke-linecap="round"/>',
        f'<path d="M373.6,164 L378,156 L382.4,164 Z" fill="{WARN}"/>',
        caption(500, 276, t["l.retry"], WARN, 9.5, anchor="middle"),
    ]

    # pending → skipped, and skipped back into flight after a replan
    out += [
        arrow(154, 154, 154, 226, "#b9b0a0", 1.5),
        caption(164, 194, t["l.depfail"], "#b9b0a0", 9.5),
        arrow(218, 243, 330, 160, ACCENT, 1.5, dashed=True, curve=14),
        caption(238, 224, t["l.replan"], ACCENT, 9.5),
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
        caption(740, 85, t["l.terminal"], INK3, 9.5),
    ]
    out.append("</svg>")
    return "\n".join(out)


# ════════════════════════════════════════════════════════════════════════════
#  Figure 5 — the four zones of a session
# ════════════════════════════════════════════════════════════════════════════

def session_zones(lang: str = "en") -> str:
    t = TEXT[lang]
    width, height = 820, 306
    out = frame(
        width, height,
        t["z.title"],
        t["z.aria"],
        lang,
    )

    out.append(caption(24, 34, t["z.head"], INK3, 10))

    zone_x, zone_w = 300, 400
    rows = [
        ("uploads/", "upload:<id>", t["z.uploads"], ACCENT, "#f6f9f7", 58),
        ("artifacts/", "artifact:<id>", t["z.artifacts"], RUN, "#f4f8fc", 116),
        ("scratch/", "scratch:<path>", t["z.scratch"], "#6d5bb5", "#f7f5fb", 174),
        ("control/", t["z.noref"], t["z.control"], ERR, "#fdf6f5", 232),
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
        (t["z.user"], 58, ACCENT, t["z.user.v"], 0),
        (t["z.engine"], 116, RUN, t["z.engine.v"], 1),
        (t["k.agent"], 174, "#6d5bb5", t["z.agent.v"], 2),
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
        caption(196, 108, t["z.reads"], "#6d5bb5", 9),
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
        ("workbench-tour", workbench),
        ("plan-execution", hero),
        ("step-kinds", step_kinds),
        ("step-lifecycle", step_lifecycle),
        ("session-zones", session_zones),
    ):
        for lang in TEXT:
            suffix = "" if lang == "en" else f".{lang}"
            target = here / f"{name}{suffix}.svg"
            target.write_text(builder(lang))
            print(f"wrote {target.name} ({target.stat().st_size:,} bytes)")
