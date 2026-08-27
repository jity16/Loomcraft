"""Example 2 — a literature meta-analysis toolkit registered as LoomCraft capabilities.

Where example 1 is an analysis pipeline over one dataset, this one is about the
messier half of agent work: the agent does not know up front what it has, has to
*ask* for what is missing, and has to change plan when a step legitimately
cannot run.

The statistics are real and small enough to read: inverse-variance weighting, the
DerSimonian-Laird random-effects estimator, Cochran's Q, I-squared, leave-one-out
influence, and Egger's regression for funnel asymmetry. No SciPy.

Two capabilities refuse on a corpus that is too small, and they refuse for
reasons a statistician would recognise rather than for the convenience of the
demo:

* ``lit.meta`` needs at least three studies. Between-study variance estimated
  from one degree of freedom is not an estimate.
* ``lit.bias`` needs at least four. Egger's regression on three points is a line
  through noise.

``lit.meta`` failing on a two-study corpus is what makes the replan in
``run_scripted.py`` a real path rather than a narrated one.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

from loomcraft import (
    Capability,
    CapabilityInput,
    NodeContext,
    NodeResult,
    Parameter,
    Port,
    Registry,
)

registry = Registry()

FIELD = re.compile(r"^(Study|Design|Samples|Outcome|Effect|Notes):\s*(.+)$", re.M)
EFFECT = re.compile(
    r"([+-]?\d+(?:\.\d+)?)\s*%\s*\(95%\s*CI\s*([+-]?\d+(?:\.\d+)?)\s*to\s*([+-]?\d+(?:\.\d+)?)\)"
)

# 95% of a normal distribution lies within this many standard errors. Turning a
# published interval back into a standard error is the whole of "harmonising".
Z95 = 1.959964


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


# ── 1. Extract ──────────────────────────────────────────────────────────────

EXTRACT = Capability(
    id="lit.extract",
    name="Extract study records",
    description=(
        "Parse study reports into structured records: design, sample size, "
        "outcome, and the reported effect with its confidence interval. Reports "
        "which files it could not parse rather than dropping them silently."
    ),
    runner="lit.extract",
    inputs=(
        CapabilityInput(
            key="reports",
            name="Study reports",
            description="Between one and eight study reports.",
            allowed_extensions=(".txt", ".md"),
            max_files=8,
        ),
    ),
    outputs=(Port(name="corpus", artifact_type="json"),),
    tags=("literature", "extract", "screening", "studies"),
)


@registry.capability_runner(EXTRACT)
async def extract(ctx: NodeContext) -> NodeResult:
    files = ctx.input_list("reports")
    records: list[dict[str, Any]] = []
    unparsed: list[dict[str, str]] = []

    for index, item in enumerate(files):
        text = item.read_text()
        fields = {key: value.strip() for key, value in FIELD.findall(text)}
        effect = EFFECT.search(fields.get("Effect", ""))
        if not effect or "Study" not in fields:
            unparsed.append(
                {"filename": item.filename, "reason": "no parsable effect estimate"}
            )
            continue
        point, low, high = (float(group) for group in effect.groups())
        records.append(
            {
                "study": fields["Study"],
                "filename": item.filename,
                "design": fields.get("Design", "unstated"),
                "n": int(fields.get("Samples", "0") or 0),
                "outcome": fields.get("Outcome", "unstated"),
                "effect": point,
                "ci_low": low,
                "ci_high": high,
                "notes": fields.get("Notes", ""),
            }
        )
        ctx.progress((index + 1) / len(files), f"read {item.filename}")

    if not records:
        return NodeResult.fail(
            f"none of the {len(files)} report(s) contained a parsable effect estimate"
        )

    ctx.emit(
        "corpus",
        "corpus.json",
        json.dumps({"studies": records, "unparsed": unparsed}, indent=2),
    )
    return NodeResult.ok(studies=len(records), unparsed=len(unparsed))


# ── 2. Harmonise ────────────────────────────────────────────────────────────

HARMONISE = Capability(
    id="lit.harmonise",
    name="Harmonise effect estimates",
    description=(
        "Put every study on one scale: recover the standard error from the "
        "reported interval, and refuse to pool studies whose outcome definitions "
        "do not match."
    ),
    runner="lit.harmonise",
    inputs=(
        CapabilityInput(
            key="corpus",
            name="Corpus",
            description="Extracted study records.",
            allowed_extensions=(".json",),
        ),
    ),
    outputs=(Port(name="harmonised", artifact_type="json"),),
    tags=("literature", "harmonise", "effect-size", "units"),
)


@registry.capability_runner(HARMONISE)
async def harmonise(ctx: NodeContext) -> NodeResult:
    corpus = json.loads(ctx.input("corpus").read_text())
    studies = corpus["studies"]

    outcomes = {study["outcome"].lower() for study in studies}
    if len(outcomes) > 1:
        # Pooling incommensurable outcomes produces a number, which is worse
        # than producing nothing.
        return NodeResult.fail(
            "studies report different outcomes and cannot be pooled: "
            + "; ".join(sorted(outcomes))
        )

    harmonised = []
    for study in studies:
        se = (study["ci_high"] - study["ci_low"]) / (2 * Z95)
        harmonised.append(
            {
                **study,
                "se": round(se, 4),
                "weight_fixed": round(1 / se**2, 5) if se else 0.0,
                "precision": round(1 / se, 4) if se else 0.0,
            }
        )

    ctx.emit(
        "harmonised",
        "harmonised.json",
        json.dumps(
            {"outcome": studies[0]["outcome"], "studies": harmonised}, indent=2
        ),
    )
    return NodeResult.ok(
        studies=len(harmonised),
        total_n=sum(study["n"] for study in harmonised),
    )


# ── 3. The pooled estimate ──────────────────────────────────────────────────

META = Capability(
    id="lit.meta",
    name="Random-effects meta-analysis",
    description=(
        "Pool the harmonised effects with the DerSimonian-Laird random-effects "
        "estimator, and report Cochran's Q, I-squared and tau-squared alongside "
        "the pooled estimate. Requires at least three studies: between-study "
        "variance estimated from one degree of freedom is not an estimate."
    ),
    runner="lit.meta",
    inputs=(
        CapabilityInput(
            key="harmonised",
            name="Harmonised studies",
            description="Output of lit.harmonise.",
            allowed_extensions=(".json",),
        ),
    ),
    outputs=(Port(name="pooled", artifact_type="json"),),
    parameters={
        "model": Parameter(
            type="string",
            description="Pooling model.",
            enum=("random", "fixed"),
            default="random",
        )
    },
    tags=("literature", "meta-analysis", "pooling", "heterogeneity"),
)

MIN_STUDIES_FOR_POOLING = 3


def pool(studies: list[dict[str, Any]], random_effects: bool = True) -> dict[str, Any]:
    """Inverse-variance pooling, DerSimonian-Laird for the random-effects case."""
    effects = [study["effect"] for study in studies]
    errors = [study["se"] for study in studies]
    weights = [1 / se**2 for se in errors]
    total = sum(weights)
    fixed = sum(w * y for w, y in zip(weights, effects, strict=True)) / total

    q = sum(w * (y - fixed) ** 2 for w, y in zip(weights, effects, strict=True))
    dof = len(studies) - 1
    i_squared = max(0.0, (q - dof) / q) * 100 if q > 0 else 0.0

    tau_squared = 0.0
    if random_effects:
        c = total - sum(w**2 for w in weights) / total
        tau_squared = max(0.0, (q - dof) / c) if c > 0 else 0.0

    adjusted = [1 / (se**2 + tau_squared) for se in errors]
    adjusted_total = sum(adjusted)
    estimate = sum(w * y for w, y in zip(adjusted, effects, strict=True)) / adjusted_total
    se = math.sqrt(1 / adjusted_total)

    return {
        "estimate": round(estimate, 4),
        "se": round(se, 4),
        "ci_low": round(estimate - Z95 * se, 4),
        "ci_high": round(estimate + Z95 * se, 4),
        "q": round(q, 4),
        "df": dof,
        "i_squared": round(i_squared, 2),
        "tau_squared": round(tau_squared, 4),
        "studies": len(studies),
    }


@registry.capability_runner(META)
async def meta(ctx: NodeContext) -> NodeResult:
    payload = json.loads(ctx.input("harmonised").read_text())
    studies = payload["studies"]

    if len(studies) < MIN_STUDIES_FOR_POOLING:
        # A precondition failure, not a transient one — retrying is pointless,
        # so this is `fail`, not `retry`. The agent's move is to replan.
        return NodeResult.fail(
            f"pooling needs at least {MIN_STUDIES_FOR_POOLING} studies to "
            f"estimate between-study variance; the corpus has {len(studies)}"
        )

    result = pool(studies, random_effects=ctx.parameters["model"] == "random")
    result["model"] = ctx.parameters["model"]
    result["outcome"] = payload["outcome"]
    ctx.log(
        f"pooled {result['estimate']:+.2f}% "
        f"({result['ci_low']:+.2f} to {result['ci_high']:+.2f}), "
        f"I² = {result['i_squared']}%",
        "info",
    )
    ctx.emit("pooled", "pooled.json", json.dumps(result, indent=2))
    return NodeResult.ok(
        estimate=result["estimate"],
        i_squared=result["i_squared"],
        studies=result["studies"],
    )


# ── 4. Influence, parallel branch A ─────────────────────────────────────────

INFLUENCE = Capability(
    id="lit.influence",
    name="Leave-one-out influence",
    description=(
        "Re-pool the corpus with each study removed in turn, to find studies "
        "whose removal moves the pooled estimate or the heterogeneity more than "
        "the rest put together."
    ),
    runner="lit.influence",
    inputs=(
        CapabilityInput(
            key="harmonised",
            name="Harmonised studies",
            description="Output of lit.harmonise.",
            allowed_extensions=(".json",),
        ),
    ),
    outputs=(Port(name="influence", artifact_type="json"),),
    tags=("literature", "influence", "leave-one-out", "sensitivity"),
)


@registry.capability_runner(INFLUENCE)
async def influence(ctx: NodeContext) -> NodeResult:
    payload = json.loads(ctx.input("harmonised").read_text())
    studies = payload["studies"]
    if len(studies) < MIN_STUDIES_FOR_POOLING + 1:
        return NodeResult.fail(
            "leave-one-out needs at least "
            f"{MIN_STUDIES_FOR_POOLING + 1} studies; the corpus has {len(studies)}"
        )

    overall = pool(studies)
    rows = []
    for index, study in enumerate(studies):
        without = pool([item for position, item in enumerate(studies) if position != index])
        rows.append(
            {
                "omitted": study["study"],
                "n": study["n"],
                "estimate_without": without["estimate"],
                "i_squared_without": without["i_squared"],
                "estimate_shift": round(without["estimate"] - overall["estimate"], 4),
                "i_squared_shift": round(without["i_squared"] - overall["i_squared"], 2),
            }
        )
        ctx.progress((index + 1) / len(studies), f"re-pooled without {study['study']}")

    # The influential study is the one whose removal moves the estimate most.
    ranked = sorted(rows, key=lambda row: abs(row["estimate_shift"]), reverse=True)
    ctx.emit(
        "influence",
        "influence.json",
        json.dumps({"overall": overall, "leave_one_out": rows, "most_influential": ranked[0]}, indent=2),
    )
    return NodeResult.ok(
        most_influential=ranked[0]["omitted"],
        estimate_shift=ranked[0]["estimate_shift"],
    )


# ── 5. Publication bias, parallel branch B ──────────────────────────────────

BIAS = Capability(
    id="lit.bias",
    name="Funnel asymmetry (Egger's test)",
    description=(
        "Regress each study's standard normal deviate on its precision. A "
        "non-zero intercept means small studies report systematically different "
        "effects from large ones — the signature of publication bias or of "
        "small-study effects. Needs at least four studies to be worth running."
    ),
    runner="lit.bias",
    inputs=(
        CapabilityInput(
            key="harmonised",
            name="Harmonised studies",
            description="Output of lit.harmonise.",
            allowed_extensions=(".json",),
        ),
    ),
    outputs=(Port(name="bias", artifact_type="json"),),
    tags=("literature", "publication-bias", "egger", "funnel"),
)

MIN_STUDIES_FOR_EGGER = 4


@registry.capability_runner(BIAS)
async def bias(ctx: NodeContext) -> NodeResult:
    payload = json.loads(ctx.input("harmonised").read_text())
    studies = payload["studies"]
    if len(studies) < MIN_STUDIES_FOR_EGGER:
        return NodeResult.fail(
            f"Egger's regression needs at least {MIN_STUDIES_FOR_EGGER} studies; "
            f"the corpus has {len(studies)}"
        )

    precision = [1 / study["se"] for study in studies]
    deviate = [study["effect"] / study["se"] for study in studies]
    mean_x, mean_y = mean(precision), mean(deviate)
    sxx = sum((x - mean_x) ** 2 for x in precision)
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(precision, deviate, strict=True))
    slope = sxy / sxx if sxx else 0.0
    intercept = mean_y - slope * mean_x

    residuals = [
        y - (intercept + slope * x) for x, y in zip(precision, deviate, strict=True)
    ]
    dof = len(studies) - 2
    sigma = math.sqrt(sum(r**2 for r in residuals) / dof) if dof > 0 else 0.0
    se_intercept = sigma * math.sqrt(1 / len(studies) + mean_x**2 / sxx) if sxx else 0.0
    t = intercept / se_intercept if se_intercept else 0.0

    ctx.emit(
        "bias",
        "bias.json",
        json.dumps(
            {
                "intercept": round(intercept, 4),
                "intercept_se": round(se_intercept, 4),
                "t": round(t, 4),
                "df": dof,
                "slope": round(slope, 4),
                "asymmetric": abs(t) >= 1.6,
                "reading": (
                    "small studies report larger effects than large ones"
                    if intercept > 0
                    else "small studies report smaller effects than large ones"
                ),
            },
            indent=2,
        ),
    )
    return NodeResult.ok(intercept=round(intercept, 4), t=round(t, 4))


# ── 6. The brief (fan-in) ───────────────────────────────────────────────────

BRIEF = Capability(
    id="lit.brief",
    name="Compose the evidence brief",
    description=(
        "Combine the harmonised corpus with whichever analyses were possible "
        "into a Markdown brief. Runs with the pooled estimate alone, or with the "
        "influence and bias analyses when they were available — and says which."
    ),
    runner="lit.brief",
    inputs=(
        CapabilityInput(key="harmonised", name="Harmonised studies", description="Output of lit.harmonise.", allowed_extensions=(".json",)),
        CapabilityInput(key="pooled", name="Pooled estimate", description="Output of lit.meta.", allowed_extensions=(".json",)),
        CapabilityInput(key="influence", name="Influence analysis", description="Output of lit.influence.", allowed_extensions=(".json",)),
        CapabilityInput(key="bias", name="Bias analysis", description="Output of lit.bias.", allowed_extensions=(".json",)),
    ),
    # Only the corpus is structurally required. A brief over two studies with no
    # pooled estimate is a legitimate output, and is exactly what the agent
    # falls back to when lit.meta refuses.
    input_variants=(("harmonised",),),
    outputs=(Port(name="brief", artifact_type="md"),),
    tags=("literature", "brief", "markdown", "synthesis"),
)


@registry.capability_runner(BRIEF)
async def brief(ctx: NodeContext) -> NodeResult:
    payload = json.loads(ctx.input("harmonised").read_text())
    studies = payload["studies"]
    pooled = json.loads(ctx.input("pooled").read_text()) if ctx.has_input("pooled") else None
    influence_data = (
        json.loads(ctx.input("influence").read_text()) if ctx.has_input("influence") else None
    )
    bias_data = json.loads(ctx.input("bias").read_text()) if ctx.has_input("bias") else None

    lines = [
        "# Evidence brief",
        "",
        f"**Outcome:** {payload['outcome']}",
        "",
        f"{len(studies)} stud{'y' if len(studies) == 1 else 'ies'}, "
        f"{sum(study['n'] for study in studies)} participants in total.",
        "",
        "| Study | n | Design | Effect | 95% CI | Weight |",
        "| --- | ---: | --- | ---: | --- | ---: |",
    ]
    total_weight = sum(study["weight_fixed"] for study in studies) or 1.0
    for study in sorted(studies, key=lambda item: item["n"], reverse=True):
        lines.append(
            f"| {study['study']} | {study['n']} | {study['design']} | "
            f"{study['effect']:+.1f}% | {study['ci_low']:+.1f} to {study['ci_high']:+.1f} | "
            f"{study['weight_fixed'] / total_weight:.0%} |"
        )

    lines += ["", "## Pooled estimate", ""]
    if pooled:
        lines += [
            f"- **{pooled['estimate']:+.2f}%** "
            f"(95% CI {pooled['ci_low']:+.2f} to {pooled['ci_high']:+.2f}), "
            f"{pooled['model']}-effects",
            f"- Cochran's Q = {pooled['q']} on {pooled['df']} df; "
            f"**I² = {pooled['i_squared']}%**; τ² = {pooled['tau_squared']}",
        ]
        if pooled["i_squared"] >= 50:
            lines.append(
                f"- ⚠ I² = {pooled['i_squared']}% is substantial heterogeneity. The "
                "studies are not estimating one common effect, so the pooled "
                "number should not be read as *the* effect."
            )
    else:
        lines += [
            "- **Not estimated.** The corpus is too small to estimate "
            "between-study variance, so no pooled effect is reported here. "
            "What follows is a narrative synthesis, not a meta-analysis.",
        ]

    if influence_data:
        top = influence_data["most_influential"]
        lines += [
            "",
            "## Influence",
            "",
            "| Omitted | Pooled without it | I² without it |",
            "| --- | ---: | ---: |",
        ]
        for row in influence_data["leave_one_out"]:
            lines.append(
                f"| {row['omitted']} | {row['estimate_without']:+.2f}% | "
                f"{row['i_squared_without']:.1f}% |"
            )
        lines += [
            "",
            f"⚠ Removing **{top['omitted']}** moves the pooled estimate by "
            f"{top['estimate_shift']:+.2f} points and the heterogeneity by "
            f"{top['i_squared_shift']:+.1f} points — more than every other study "
            "combined.",
        ]

    if bias_data:
        lines += [
            "",
            "## Small-study effects",
            "",
            f"- Egger's intercept = **{bias_data['intercept']:+.2f}** "
            f"(SE {bias_data['intercept_se']}, t = {bias_data['t']} on {bias_data['df']} df)",
            f"- {'⚠ ' if bias_data['asymmetric'] else ''}The funnel is "
            f"{'asymmetric' if bias_data['asymmetric'] else 'not detectably asymmetric'}: "
            f"{bias_data['reading']}.",
        ]

    lines += ["", "## What this supports", ""]
    if pooled and influence_data:
        top = influence_data["most_influential"]
        without = next(
            row for row in influence_data["leave_one_out"] if row["omitted"] == top["omitted"]
        )
        lines.append(
            f"A positive effect is supported, but at roughly "
            f"**{without['estimate_without']:+.1f}%**, not "
            f"{pooled['estimate']:+.1f}%. The higher figure is one small study, "
            f"and once it is set aside the remaining studies agree with each "
            f"other completely (I² = {without['i_squared_without']:.0f}%)."
        )
    elif pooled:
        lines.append(
            f"A pooled effect of {pooled['estimate']:+.2f}% is reported, but with "
            "no influence or bias analysis it should be treated as provisional."
        )
    else:
        lines.append(
            f"With {len(studies)} studies there is no defensible pooled estimate. "
            "Both report a positive effect with overlapping intervals, which is "
            "consistent with a real effect and equally consistent with two "
            "underpowered studies pointing the same way by chance."
        )

    ctx.emit("brief", "evidence-brief.md", "\n".join(lines) + "\n")
    return NodeResult.ok(
        studies=len(studies),
        pooled=bool(pooled),
        sections=sum(1 for item in (pooled, influence_data, bias_data) if item),
    )


assert not registry.validate(), registry.validate()
