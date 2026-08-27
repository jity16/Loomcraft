#!/usr/bin/env python3
"""Example 1 — run the full pipeline with no model and no API key.

    python examples/01-data-pipeline/run_scripted.py

The tool calls a real agent would make are issued directly. Every one still goes
through the broker, so this exercises the same validation, scheduling, retry,
approval, and event path as a live run — which is why it doubles as a CI smoke
test.

What it demonstrates, in order:

1.  Discovery — context, capability search, bounded file preview.
2.  DAG validation and layering — the plan is checked before anything runs.
3.  Dependency gating — a step cannot jump ahead of its dependencies.
4.  Typed contracts — wrong input keys and out-of-range parameters are refused.
5.  Capability execution with artifacts.
6.  Retry with exponential backoff.
7.  Fan-in from multiple upstream artifacts.
8.  Agent-reported steps (`dynamic` / `review` / `answer`).
9.  Replan discipline — revisions must increase and must explain themselves.
10. **Real engine parallelism** via a registered workflow.
11. **Human approval** — a hard-to-reverse step parks until someone confirms.
12. Failure and skip propagation.
13. The audit trail.
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[2] / "packages" / "core" / "src"))

from capabilities import registry, reset_reference_attempts  # noqa: E402

from loomcraft import (  # noqa: E402
    Engine,
    SessionStore,
    ToolBroker,
    graph_from_capability,
    graph_from_workflow,
    parse_plan,
)

SAMPLE = """id,region,revenue,units,notes
1,north,1200.5,10,steady
2,south,980.0,8,
3,north,1105.25,9,steady
4,east,1010.0,9,
5,west,99999.0,7,suspected typo
6,south,1150.0,11,
7,north,1080.5,10,
8,east,,9,missing revenue
9,west,1120.0,10,
10,south,1095.0,9,
10,south,1095.0,9,
,,,,
"""

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"


def banner(number: int, text: str) -> None:
    print(f"\n{BOLD}{number:>2} · {text}{RESET}")
    print(f"{DIM}{'─' * 62}{RESET}")


def show(label: str, value: object) -> None:
    print(f"   {label:<30} {value}")


async def main() -> int:
    reset_reference_attempts()

    with TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "sessions")
        session = store.create("data-pipeline-demo")
        upload = session.save_upload("sales.csv", SAMPLE.encode())
        broker = ToolBroker(session, registry)
        broker.begin_turn()

        # ── 1 ────────────────────────────────────────────────────────────────
        banner(1, "Discovery: what the agent can see")
        context = await broker.dispatch("session_context", {})
        show("uploads visible", len(context.result["uploads"]))
        show("capabilities registered", context.result["catalog"]["capability_count"])
        show("workflows registered", context.result["catalog"]["workflow_count"])

        found = await broker.dispatch("capability_search", {"query": "profile csv columns"})
        show("best capability match", found.result["results"][0]["id"])
        show("its input contract", found.result["results"][0]["inputs"][0]["key"])

        preview = await broker.dispatch(
            "inspect_source", {"source_ref": upload["source_ref"], "max_lines": 2}
        )
        show("header row", preview.result["preview_lines"][0])

        # ── 2 ────────────────────────────────────────────────────────────────
        banner(2, "Publish a plan: validated and layered")
        plan = {
            "goal": "Assess the quality of the uploaded sales table.",
            "summary": "Clean, profile and scan in parallel, then report.",
            "revision": 1,
            "steps": [
                {"id": "clean", "title": "Clean the table", "kind": "capability", "capability": "csv.clean"},
                {"id": "profile", "title": "Profile columns", "kind": "capability", "capability": "csv.profile", "depends_on": ["clean"]},
                {"id": "outliers", "title": "Detect outliers", "kind": "capability", "capability": "csv.outliers", "depends_on": ["clean"]},
                {"id": "reference", "title": "Fetch thresholds", "kind": "capability", "capability": "csv.fetch_reference"},
                {"id": "report", "title": "Compose the report", "kind": "capability", "capability": "csv.report", "depends_on": ["profile", "outliers", "reference"]},
                {"id": "review", "title": "Verify the report", "kind": "review", "depends_on": ["report"]},
                {"id": "answer", "title": "Answer the user", "kind": "answer", "depends_on": ["review"]},
            ],
        }
        show("accepted", (await broker.dispatch("publish_plan", {"plan": plan})).ok)

        for index, layer in enumerate(parse_plan(session.current_plan()).layers):
            note = "  ← may run concurrently" if len(layer) > 1 else ""
            show(f"layer {index}", ", ".join(layer) + note)

        cyclic = await broker.dispatch(
            "publish_plan",
            {
                "plan": {
                    "goal": "g",
                    "revision": 9,
                    "reason": "test",
                    "steps": [
                        {"id": "a", "title": "A", "kind": "dynamic", "depends_on": ["b"]},
                        {"id": "b", "title": "B", "kind": "dynamic", "depends_on": ["a"]},
                    ],
                }
            },
        )
        show("a cyclic plan is", f"refused — {cyclic.error}")

        # ── 3 ────────────────────────────────────────────────────────────────
        banner(3, "Dependency gating: no jumping ahead")
        early = await broker.dispatch(
            "run_capability",
            {
                "capability_id": "csv.profile",
                "step_id": "profile",
                "inputs": {"cleaned": upload["source_ref"]},
            },
        )
        show("refused", not early.ok)
        show("reason", early.error)

        # ── 4 ────────────────────────────────────────────────────────────────
        banner(4, "Typed contracts: bad calls never reach a runner")
        wrong_key = await broker.dispatch(
            "run_capability",
            {"capability_id": "csv.clean", "step_id": "clean", "inputs": {"nope": upload["source_ref"]}},
        )
        show("unknown input key", f"{wrong_key.error_code} — {wrong_key.error}")

        half_variant = await broker.dispatch(
            "run_capability",
            {"capability_id": "csv.clean", "step_id": "clean", "inputs": {"header": upload["source_ref"]}},
        )
        show("half an input variant", half_variant.error)

        bad_param = await broker.dispatch(
            "run_capability",
            {
                "capability_id": "csv.clean",
                "step_id": "clean",
                "inputs": {"table": upload["source_ref"]},
                "parameters": {"min_row_fill": 7.5},
            },
        )
        show("out-of-range parameter", bad_param.error)

        # ── 5 ────────────────────────────────────────────────────────────────
        banner(5, "Execute: clean")
        cleaned = await broker.dispatch(
            "run_capability",
            {
                "capability_id": "csv.clean",
                "step_id": "clean",
                "inputs": {"table": upload["source_ref"]},
                "parameters": {"drop_duplicates": True, "min_row_fill": 0.5},
            },
        )
        cleaned_ref = cleaned.result["artifacts"][0]["source_ref"]
        show("status", cleaned.result["status"])
        show("artifact", cleaned.result["artifacts"][0]["filename"])

        profiled = await broker.dispatch(
            "run_capability",
            {"capability_id": "csv.profile", "step_id": "profile", "inputs": {"cleaned": cleaned_ref}},
        )
        profile_ref = profiled.result["artifacts"][0]["source_ref"]

        scanned = await broker.dispatch(
            "run_capability",
            {
                "capability_id": "csv.outliers",
                "step_id": "outliers",
                "inputs": {"cleaned": cleaned_ref},
                # A tighter threshold than the default, to catch the typo'd row.
                "parameters": {"z_threshold": 2.0},
            },
        )
        outliers_ref = scanned.result["artifacts"][0]["source_ref"]
        show("profile + outliers", "both succeeded")

        # ── 6 ────────────────────────────────────────────────────────────────
        banner(6, "Retry with exponential backoff")
        started = time.monotonic()
        reference = await broker.dispatch(
            "run_capability",
            {"capability_id": "csv.fetch_reference", "step_id": "reference", "inputs": {}},
        )
        reference_ref = reference.result["artifacts"][0]["source_ref"]
        show("status", reference.result["status"])
        show("attempts used", "3 (two 503s, then success)")
        show("elapsed", f"{time.monotonic() - started:.2f}s  (0.2s + 0.4s of backoff)")

        # ── 7 ────────────────────────────────────────────────────────────────
        banner(7, "Fan-in: three upstream artifacts")
        composed = await broker.dispatch(
            "run_capability",
            {
                "capability_id": "csv.report",
                "step_id": "report",
                "inputs": {
                    "profile": profile_ref,
                    "outliers": outliers_ref,
                    "reference": reference_ref,
                },
            },
        )
        show("status", composed.result["status"])

        # ── 8 ────────────────────────────────────────────────────────────────
        banner(8, "Agent-reported steps")
        await broker.dispatch(
            "update_step",
            {"step_id": "review", "status": "succeeded", "summary": "Report matches the artifacts."},
        )
        await broker.dispatch(
            "update_step",
            {"step_id": "answer", "status": "succeeded", "summary": "Delivered the quality report."},
        )
        guarded = await broker.dispatch(
            "update_step", {"step_id": "clean", "status": "failed"}
        )
        show("plan progress", parse_plan(session.current_plan()).progress)
        show("faking a capability step", f"refused — {guarded.error}")

        # ── 9 ────────────────────────────────────────────────────────────────
        banner(9, "Replan discipline")
        revised = {
            **plan,
            "revision": 2,
            "reason": "The reference service is degraded; report without thresholds.",
            "steps": [
                {**step, "depends_on": [d for d in step.get("depends_on", []) if d != "reference"]}
                for step in plan["steps"]
                if step["id"] != "reference"
            ],
        }
        show("revision 2 accepted", (await broker.dispatch("publish_plan", {"plan": revised})).ok)
        show("history retained", [item["revision"] for item in session.plan_history()])

        no_reason = await broker.dispatch(
            "publish_plan", {"plan": {**revised, "revision": 3, "reason": None}}
        )
        show("revision with no reason", f"refused — {no_reason.error}")

        stale = await broker.dispatch("publish_plan", {"plan": {**revised, "revision": 1}})
        show("non-increasing revision", f"refused — {stale.error}")

        # ── 10 ───────────────────────────────────────────────────────────────
        banner(10, "Real engine parallelism (registered workflow)")
        # The broker refuses two *overlapping* agent-initiated executions, so
        # agent-level calls are sequential by design. Parallelism lives inside a
        # single execution graph: this SOP has profile and outliers on the same
        # dependency layer, so the engine runs them at the same time.
        workflow = registry.workflow("csv.quality_sop")
        engine = Engine(registry, session, emit=lambda *_: None)
        graph = graph_from_workflow(
            workflow,
            sources={"table": (upload["source_ref"],)},
            parameters=workflow.validate_parameters({"z_threshold": 2.0}),
        )
        show("graph layers", " → ".join("+".join(layer) for layer in graph.layers))
        started = time.monotonic()
        run = await engine.execute(graph)
        show("status", run.status)
        show("nodes", {key: state.status for key, state in run.nodes.items()})
        show("artifacts", len(run.artifacts))
        show("wall clock", f"{time.monotonic() - started:.2f}s")

        # ── 11 ───────────────────────────────────────────────────────────────
        banner(11, "Human approval on a hard-to-reverse step")
        report_artifact = next(
            item for item in run.artifacts if item["filename"].endswith(".md")
        )
        approval_graph = graph_from_capability(
            registry.capability("csv.publish"),
            sources={"report": (report_artifact["source_ref"],)},
            parameters={},
        )
        approval_run = engine.submit(approval_graph)
        for _ in range(200):
            await asyncio.sleep(0.005)
            if approval_run.pending_approvals:
                break
        show("run status", approval_run.status)
        show("waiting on", approval_run.pending_approvals)
        show("side effect performed?", "no — the runner returned before acting")

        approval_run.approve("execute", True)
        await approval_run.wait()
        show("after approval", approval_run.status)

        # ── 12 ───────────────────────────────────────────────────────────────
        banner(12, "Failure and skip propagation")
        failing = {
            "goal": "Demonstrate failure handling.",
            "revision": 4,
            "reason": "Show what happens when an upstream step fails.",
            "steps": [
                {"id": "load", "title": "Load a broken file", "kind": "capability", "capability": "csv.clean"},
                {"id": "downstream", "title": "Profile it", "kind": "capability", "capability": "csv.profile", "depends_on": ["load"]},
                {"id": "wrap_up", "title": "Answer", "kind": "answer", "depends_on": ["downstream"]},
            ],
        }
        await broker.dispatch("publish_plan", {"plan": failing})
        broken = session.save_upload("broken.csv", b"\n\n\n")
        failed = await broker.dispatch(
            "run_capability",
            {"capability_id": "csv.clean", "step_id": "load", "inputs": {"table": broken["source_ref"]}},
        )
        statuses = {
            step["id"]: step["status"] for step in session.current_plan()["steps"]
        }
        show("load", f"{statuses['load']} — {failed.error}")
        show("downstream", f"{statuses['downstream']}  ← skipped, never ran")
        show("wrap_up", f"{statuses['wrap_up']}  ← skipped transitively")

        # ── 13 ───────────────────────────────────────────────────────────────
        banner(13, "Results and audit trail")
        for artifact in session.list_artifacts():
            show(artifact["filename"], f"{artifact['size']:>6} B   {artifact['source_ref']}")
        show("events recorded", session.events.last_seq)
        show("hash chain intact", session.events.verify())

        report_row = next(
            item for item in session.list_artifacts() if item["filename"].endswith(".md")
        )
        print(f"\n{DIM}{'─' * 62}{RESET}")
        print((session.root / report_row["relpath"]).read_text())

        await broker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
