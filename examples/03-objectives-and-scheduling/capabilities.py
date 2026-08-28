"""Example 3 — a small field-trial toolkit, registered as LoomCraft capabilities.

The numbers are computed, not fixtured, but they are deliberately simple: this
example is about the *engine's* behaviour, not the statistics. Example 1 is the
one to read for a real analysis.

Each capability here exists to exercise one thing the scheduler has to get right:

* ``trial.clean`` is the root everything else hangs off.
* ``trial.yield_scan`` and ``trial.stability`` depend only on the root and not on
  each other, so a plan containing both gets **genuine concurrency**.
* ``trial.maternal_scan`` fails for a real reason — the pedigree it is handed has
  no dam column — which is what makes ``on_failure: "continue"`` meaningful
  rather than decorative.
* ``trial.annotate`` fails twice before succeeding, exercising **retry with
  backoff** driven from the plan.
* ``review.calibration`` uses a ``review.`` runner prefix, so a plan may bind it
  to a ``review`` step and have the **server** own the verdict.
"""

from __future__ import annotations

import json

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

# Deterministic stand-in for a flaky external service.
_annotate_attempts: dict[str, int] = {}


def _rows(text: str) -> list[dict[str, str]]:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    header = lines[0].split("\t")
    return [dict(zip(header, line.split("\t"))) for line in lines[1:]]


# ── Root ────────────────────────────────────────────────────────────────────

CLEAN = Capability(
    id="trial.clean",
    name="Clean the trial table",
    description="Drop rows with a missing yield and standardise column names.",
    runner="trial.clean",
    inputs=(
        CapabilityInput(
            key="trial",
            name="Trial table",
            description="Tab-separated plot-level records.",
            allowed_extensions=(".tsv",),
        ),
    ),
    outputs=(Port(name="clean", artifact_type="tsv"),),
    tags=("trial", "clean", "qc"),
)


@registry.capability_runner(CLEAN)
async def clean(ctx: NodeContext) -> NodeResult:
    rows = _rows(ctx.input("trial").path.read_text(encoding="utf-8"))
    kept = [row for row in rows if row.get("yield") not in (None, "", "NA")]
    header = "\t".join(kept[0]) if kept else "plot\tgenotype\tyield"
    body = "\n".join("\t".join(row[key] for key in kept[0]) for row in kept)
    ctx.emit("clean", "clean.tsv", f"{header}\n{body}\n")
    return NodeResult.ok(summary=f"{len(kept)} of {len(rows)} plots kept")


# ── Two independent branches off the root ───────────────────────────────────

SCAN = Capability(
    id="trial.yield_scan",
    name="Scan genotypes for a yield effect",
    description="Per-genotype mean yield against the trial mean.",
    runner="trial.yield_scan",
    inputs=(
        CapabilityInput(
            key="table",
            name="Clean table",
            description="The cleaned trial table.",
            port_name="clean",
            allowed_extensions=(".tsv",),
        ),
    ),
    parameters={
        "min_plots": Parameter(
            type="integer",
            description="Genotypes with fewer plots than this are not reported.",
            minimum=1,
            maximum=100,
            default=2,
        )
    },
    outputs=(Port(name="effects", artifact_type="json"),),
    tags=("trial", "scan", "yield"),
)


@registry.capability_runner(SCAN)
async def yield_scan(ctx: NodeContext) -> NodeResult:
    rows = _rows(ctx.input("table").path.read_text(encoding="utf-8"))
    by_genotype: dict[str, list[float]] = {}
    for row in rows:
        by_genotype.setdefault(row["genotype"], []).append(float(row["yield"]))
    overall = sum(v for values in by_genotype.values() for v in values) / max(
        1, sum(len(values) for values in by_genotype.values())
    )
    minimum = int(ctx.parameters["min_plots"])
    effects = {
        genotype: round(sum(values) / len(values) - overall, 4)
        for genotype, values in sorted(by_genotype.items())
        if len(values) >= minimum
    }
    ctx.emit("effects", "effects.json", json.dumps(effects, indent=2))
    return NodeResult.ok(summary=f"{len(effects)} genotypes above the plot minimum")


STABILITY = Capability(
    id="trial.stability",
    name="Score genotype stability",
    description="Spread of a genotype's yields across plots.",
    runner="trial.stability",
    inputs=(
        CapabilityInput(
            key="table",
            name="Clean table",
            description="The cleaned trial table.",
            port_name="clean",
            allowed_extensions=(".tsv",),
        ),
    ),
    outputs=(Port(name="stability", artifact_type="json"),),
    tags=("trial", "stability", "variance"),
)


@registry.capability_runner(STABILITY)
async def stability(ctx: NodeContext) -> NodeResult:
    rows = _rows(ctx.input("table").path.read_text(encoding="utf-8"))
    by_genotype: dict[str, list[float]] = {}
    for row in rows:
        by_genotype.setdefault(row["genotype"], []).append(float(row["yield"]))
    spread = {
        genotype: round(max(values) - min(values), 4)
        for genotype, values in sorted(by_genotype.items())
    }
    ctx.emit("stability", "stability.json", json.dumps(spread, indent=2))
    return NodeResult.ok(summary=f"spread scored for {len(spread)} genotypes")


# ── A branch that genuinely cannot be estimated ─────────────────────────────

MATERNAL = Capability(
    id="trial.maternal_scan",
    name="Estimate the maternal component",
    description=(
        "Partition yield variance into a maternal component. Needs a pedigree "
        "with dam identifiers; refuses without one rather than returning a "
        "number that is not identifiable."
    ),
    runner="trial.maternal_scan",
    inputs=(
        CapabilityInput(
            key="table",
            name="Clean table",
            description="The cleaned trial table.",
            port_name="clean",
            allowed_extensions=(".tsv",),
        ),
    ),
    outputs=(Port(name="maternal", artifact_type="json"),),
    tags=("trial", "maternal", "variance"),
)


@registry.capability_runner(MATERNAL)
async def maternal_scan(ctx: NodeContext) -> NodeResult:
    rows = _rows(ctx.input("table").path.read_text(encoding="utf-8"))
    if not rows or "dam" not in rows[0]:
        # Not a crash and not a retryable blip: the design cannot answer this.
        return NodeResult.fail(
            "the trial table has no dam column, so the maternal component is "
            "not identifiable from this design"
        )
    return NodeResult.ok(summary="maternal component estimated")


# ── A flaky external service ────────────────────────────────────────────────

ANNOTATE = Capability(
    id="trial.annotate",
    name="Annotate genotypes from the variety register",
    description="Look each genotype up in the external variety register.",
    runner="trial.annotate",
    inputs=(
        CapabilityInput(
            key="effects",
            name="Effect table",
            description="Per-genotype effects to annotate.",
            port_name="effects",
            allowed_extensions=(".json",),
        ),
    ),
    outputs=(Port(name="annotated", artifact_type="json"),),
    tags=("trial", "annotate", "register"),
)


@registry.capability_runner(ANNOTATE)
async def annotate(ctx: NodeContext) -> NodeResult:
    seen = _annotate_attempts.get(ctx.run_id, 0) + 1
    _annotate_attempts[ctx.run_id] = seen
    if seen < 3:
        # Retryable: the register is up, it is just rate-limiting us.
        return NodeResult.retry(f"variety register returned 503 (attempt {seen})")
    effects = json.loads(ctx.input("effects").path.read_text(encoding="utf-8"))
    annotated = {
        genotype: {"effect": value, "register": f"VR-{index:04d}"}
        for index, (genotype, value) in enumerate(sorted(effects.items()), start=1)
    }
    ctx.emit("annotated", "annotated.json", json.dumps(annotated, indent=2))
    return NodeResult.ok(summary=f"annotated on attempt {seen}")


# ── A review the server owns ────────────────────────────────────────────────

CALIBRATION = Capability(
    id="review.calibration",
    name="Check the effect estimates are plausible",
    description=(
        "Verification, not analysis: refuse the result if any genotype effect "
        "is implausibly large for this trial."
    ),
    # The `review.` prefix is what lets a plan bind this to a `review` step.
    runner="review.calibration",
    inputs=(
        CapabilityInput(
            key="annotated",
            name="Annotated effects",
            description="The annotated effect table.",
            port_name="annotated",
            allowed_extensions=(".json",),
        ),
    ),
    outputs=(Port(name="verdict", artifact_type="json"),),
    tags=("trial", "review", "calibration"),
)


@registry.capability_runner(CALIBRATION)
async def calibration(ctx: NodeContext) -> NodeResult:
    annotated = json.loads(ctx.input("annotated").path.read_text(encoding="utf-8"))
    worst = max((abs(row["effect"]) for row in annotated.values()), default=0.0)
    verdict = {"largest_absolute_effect": worst, "plausible": worst < 5.0}
    ctx.emit("verdict", "calibration.json", json.dumps(verdict, indent=2))
    if not verdict["plausible"]:
        return NodeResult.fail(f"effect of {worst} t/ha is not plausible for this trial")
    return NodeResult.ok(summary=f"largest effect {worst} t/ha — plausible")


__all__ = ["registry"]
