#!/usr/bin/env python3
"""A small, domain-shaped LoomCraft tour.

    python examples/00-workbench-tour/run.py

The example deliberately looks like a real analysis plan: one normalisation
step fans out into three independent branches, each branch is checked, and the
results fan back in to a report.  It runs entirely offline and uses the same
``publish_plan`` / ``execute_plan`` path a model would use.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(
    0, str(Path(__file__).resolve().parents[2] / "packages" / "core" / "src")
)

from loomcraft import (  # noqa
    Capability,
    CapabilityInput,
    Engine,
    NodeContext,
    NodeResult,
    Port,
    Registry,
    SessionStore,
    ToolBroker,
    parse_plan,
)


registry = Registry()
spans: dict[str, list[tuple[float, float]]] = {}
report_calls = 0


DATASET_INPUT = (
    CapabilityInput(
        key="dataset",
        name="Normalised dataset",
        description="A tab-separated trial table.",
        allowed_extensions=(".tsv",),
        # Downstream capabilities call this port ``dataset``; the producer
        # emits ``normalized``.  The explicit port name demonstrates how a
        # contract can rename an upstream artifact without loosening checks.
        port_name="normalized",
    ),
)
ANALYSIS_INPUT = (
    CapabilityInput(
        key="analysis",
        name="Analysis input",
        description="One prepared analysis input.",
        allowed_extensions=(".json",),
    ),
)
STATS_INPUT = (
    CapabilityInput(
        key="stats",
        name="Statistics",
        description="Statistics produced by one analysis branch.",
        allowed_extensions=(".json",),
    ),
)
QC_INPUT = (
    CapabilityInput(
        key="qc",
        name="Quality checks",
        description="Quality-check outputs from the analysis branches.",
        allowed_extensions=(".json",),
        max_files=3,
    ),
)


def register(
    capability: Capability,
    runner: Callable[[NodeContext], Awaitable[NodeResult]],
) -> None:
    registry.register_capability(capability)
    registry.register_runner(capability.runner, runner)


async def timed(
    ctx: NodeContext,
    work: Callable[[], Awaitable[NodeResult]],
) -> NodeResult:
    started = time.perf_counter()
    try:
        return await work()
    finally:
        spans.setdefault(ctx.node_id, []).append((started, time.perf_counter()))


async def normalize(ctx: NodeContext) -> NodeResult:
    async def work() -> NodeResult:
        # Reading the upload proves the first edge is a real source binding.
        rows = len(ctx.input("dataset").read_text().splitlines()) - 1
        await asyncio.sleep(0.04)
        ctx.emit(
            "normalized",
            "normalized.tsv",
            "sample\ttrait\n" + "\n".join(f"S{i}\t{i * 0.5:.1f}" for i in range(rows)),
        )
        return NodeResult.ok(summary=f"normalised {rows} rows")

    return await timed(ctx, work)


def make_preparer(
    capability_id: str,
    title: str,
    filename: str,
    summary: str,
) -> None:
    async def runner(ctx: NodeContext) -> NodeResult:
        async def work() -> NodeResult:
            ctx.input("dataset").read_bytes()
            await asyncio.sleep(0.16)
            ctx.emit("analysis", filename, '{"kind": "prepared"}')
            return NodeResult.ok(summary=summary)

        return await timed(ctx, work)

    register(
        Capability(
            id=capability_id,
            name=title,
            description=summary,
            runner=capability_id,
            inputs=DATASET_INPUT,
            outputs=(Port(name="analysis", artifact_type="json"),),
        ),
        runner,
    )


def make_scan(
    capability_id: str,
    title: str,
    filename: str,
    summary: str,
    *,
    flaky: bool = False,
) -> None:
    async def runner(ctx: NodeContext) -> NodeResult:
        async def work() -> NodeResult:
            ctx.input("analysis").read_bytes()
            await asyncio.sleep(0.12 if not flaky else 0.04)
            if flaky and ctx.attempt == 1:
                return NodeResult.retry("the statistics mirror returned 503")
            ctx.emit("stats", filename, '{"status": "computed"}')
            return NodeResult.ok(summary=summary)

        return await timed(ctx, work)

    register(
        Capability(
            id=capability_id,
            name=title,
            description=summary,
            runner=capability_id,
            inputs=ANALYSIS_INPUT,
            outputs=(Port(name="stats", artifact_type="json"),),
            max_attempts=2 if flaky else 1,
            retry_backoff_seconds=0.02,
        ),
        runner,
    )


def make_qc(capability_id: str, title: str, filename: str, summary: str) -> None:
    async def runner(ctx: NodeContext) -> NodeResult:
        async def work() -> NodeResult:
            ctx.input("stats").read_bytes()
            await asyncio.sleep(0.06)
            ctx.emit("qc", filename, '{"calibrated": true}')
            return NodeResult.ok(summary=summary)

        return await timed(ctx, work)

    register(
        Capability(
            id=capability_id,
            name=title,
            description=summary,
            runner=capability_id,
            inputs=STATS_INPUT,
            outputs=(Port(name="qc", artifact_type="json"),),
        ),
        runner,
    )


async def compose_report(ctx: NodeContext) -> NodeResult:
    global report_calls
    report_calls += 1

    async def work() -> NodeResult:
        checks = ctx.input_list("qc")
        await asyncio.sleep(0.05)
        ctx.emit(
            "report",
            "trial-report.md",
            "# Trial report\n\nThree branches checked successfully.\n",
            content_type="text/markdown",
        )
        return NodeResult.ok(summary=f"combined {len(checks)} quality checks")

    return await timed(ctx, work)


make_preparer(
    "genotype.plink_pca", "Population structure", "pca.json", "ancestry axes prepared"
)
make_preparer(
    "phenotype.prepare", "Prepare phenotype", "phenotype.json", "trait values prepared"
)
make_preparer(
    "genotype.kinship",
    "Relatedness matrix",
    "kinship.json",
    "relatedness matrix prepared",
)
make_scan("gwas.scan_yield", "GWAS · yield", "yield.json", "yield scan complete")
make_scan(
    "gwas.scan_depth",
    "GWAS · depth",
    "depth.json",
    "depth scan complete",
    flaky=True,
)
make_scan("gwas.scan_height", "GWAS · height", "height.json", "height scan complete")
make_qc("gwas.qc_yield", "QC · yield", "qc-yield.json", "yield calibrated")
make_qc("gwas.qc_depth", "QC · depth", "qc-depth.json", "depth calibrated")
make_qc("gwas.qc_height", "QC · height", "qc-height.json", "height calibrated")
register(
    Capability(
        id="report.compose",
        name="Compose report",
        description="Combine the branch checks into a markdown report.",
        runner="report.compose",
        inputs=QC_INPUT,
        outputs=(Port(name="report", artifact_type="markdown"),),
        requires_approval=True,
    ),
    compose_report,
)
register(
    Capability(
        id="genotype.variant_normalize",
        name="Normalize variants",
        description="Normalise the uploaded trial dataset.",
        runner="genotype.variant_normalize",
        inputs=DATASET_INPUT,
        outputs=(Port(name="normalized", artifact_type="table"),),
    ),
    normalize,
)


def plan(upload_ref: str) -> dict[str, object]:
    return {
        "goal": "Produce a markdown report from the trial dataset.",
        "summary": "Prepare three independent analyses, check each result, then report.",
        "revision": 1,
        "steps": [
            {
                "id": "normalize",
                "title": "Normalize variants",
                "kind": "capability",
                "capability": "genotype.variant_normalize",
            },
            {
                "id": "pca",
                "title": "Population structure",
                "kind": "capability",
                "capability": "genotype.plink_pca",
                "depends_on": ["normalize"],
            },
            {
                "id": "phenotype",
                "title": "Prepare phenotype",
                "kind": "capability",
                "capability": "phenotype.prepare",
                "depends_on": ["normalize"],
            },
            {
                "id": "kinship",
                "title": "Relatedness matrix",
                "kind": "capability",
                "capability": "genotype.kinship",
                "depends_on": ["normalize"],
            },
            {
                "id": "scan_yield",
                "title": "GWAS · yield",
                "kind": "capability",
                "capability": "gwas.scan_yield",
                "depends_on": ["pca"],
            },
            {
                "id": "scan_depth",
                "title": "GWAS · depth",
                "kind": "capability",
                "capability": "gwas.scan_depth",
                "depends_on": ["phenotype"],
            },
            {
                "id": "scan_height",
                "title": "GWAS · height",
                "kind": "capability",
                "capability": "gwas.scan_height",
                "depends_on": ["kinship"],
            },
            {
                "id": "qc_yield",
                "title": "QC · yield",
                "kind": "capability",
                "capability": "gwas.qc_yield",
                "depends_on": ["scan_yield"],
            },
            {
                "id": "qc_depth",
                "title": "QC · depth",
                "kind": "capability",
                "capability": "gwas.qc_depth",
                "depends_on": ["scan_depth"],
            },
            {
                "id": "qc_height",
                "title": "QC · height",
                "kind": "capability",
                "capability": "gwas.qc_height",
                "depends_on": ["scan_height"],
            },
            {
                "id": "report",
                "title": "Compose report",
                "kind": "capability",
                "capability": "report.compose",
                "depends_on": ["qc_yield", "qc_depth", "qc_height"],
            },
        ],
        "metadata": {"upload_ref": upload_ref},
    }


def overlap(node_ids: list[str]) -> float:
    intervals = [spans[node_id][0] for node_id in node_ids]
    return max(
        0.0, min(end for _, end in intervals) - max(start for start, _ in intervals)
    )


async def main() -> int:
    global report_calls
    spans.clear()
    report_calls = 0
    trial = (
        "sample\ttrait\n"
        + "\n".join(f"S{i}\t{i * 0.5:.1f}" for i in range(1, 7))
        + "\n"
    )

    with TemporaryDirectory() as tmp:
        session = SessionStore(Path(tmp) / "sessions", in_memory_events=True).create(
            "workbench-tour"
        )
        upload = session.save_upload("trial.tsv", trial.encode("utf-8"))
        engine = Engine(registry, session, max_parallel=8)
        broker = ToolBroker(session, registry, engine=engine)
        broker.begin_turn()

        invalid = {
            "goal": "This graph must not run.",
            "revision": 1,
            "steps": [
                {"id": "a", "title": "A", "kind": "answer", "depends_on": ["b"]},
                {"id": "b", "title": "B", "kind": "answer", "depends_on": ["a"]},
            ],
        }
        rejected = await broker.dispatch("publish_plan", {"plan": invalid})
        print(f"validation        cycle refused before execution={not rejected.ok}")

        published = await broker.dispatch(
            "publish_plan", {"plan": plan(upload["source_ref"])}
        )
        if not published.ok:
            raise RuntimeError(published.error)

        parsed = session.current_plan()
        assert parsed is not None
        layers = parse_plan(parsed).layers
        print("revision 1 · 11 steps")
        for index, layer in enumerate(layers):
            print(f"layer {index}  {' + '.join(layer)}")
        print()

        first = await broker.dispatch(
            "execute_plan",
            {"inputs": {"normalize": {"inputs": {"dataset": upload["source_ref"]}}}},
        )
        if not first.ok or first.result is None:
            raise RuntimeError(first.error)
        paused = first.result
        print(
            "parallel window  pca, phenotype, kinship  "
            f"overlap={overlap(['pca', 'phenotype', 'kinship']):.2f}s"
        )
        depth = next(
            step
            for step in session.current_plan()["steps"]
            if step["id"] == "scan_depth"
        )
        print(
            f"retry            scan.depth               attempt {depth['attempts']}/2"
        )
        print(f"approval         report                   runner calls={report_calls}")

        final = await broker.approve_run(paused["id"], "report", comment="looks good")
        if final is None or final["status"] != "succeeded":
            raise RuntimeError("approval did not finish the plan")
        statuses = {
            step["id"]: step["status"] for step in session.current_plan()["steps"]
        }
        print(
            f"run              {final['status']}                {sum(status == 'succeeded' for status in statuses.values())}/11 nodes accounted for"
        )
        print(f"report runner    invoked after approval       calls={report_calls}")
        print(
            "statuses         "
            + ", ".join(f"{key}={value}" for key, value in statuses.items())
        )
        print(f"events           {session.events.last_seq} append-only records")
        print(f"hash chain       {'verified' if session.events.verify() else 'BROKEN'}")
        await broker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
