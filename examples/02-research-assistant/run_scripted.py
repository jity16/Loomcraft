#!/usr/bin/env python3
"""Example 2 — asking for missing files, failing, and replanning around it.

    python examples/02-research-assistant/run_scripted.py

The story: the user asks for a comparative research brief but has uploaded only
one document. A well-behaved agent must

1. notice the gap and **ask** with typed slots rather than guessing,
2. be **blocked from executing** until the user responds,
3. plan for the full comparison once files arrive,
4. hit a **genuine failure** (contradiction analysis on a single-document
   corpus, when the user cancels the request instead of uploading),
5. **replan with a reason** and deliver the reduced-scope result.

Both branches are run so you can see each one end to end.

This uses :class:`ScriptedAgent`, so it needs no model and no API key — but the
tool calls are exactly the ones a real agent issues, and each goes through the
same broker. Swap in ``AnthropicAgent`` (see ``run_live.py``) and the server side
does not change at all.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[2] / "packages" / "core" / "src"))

from capabilities import registry  # noqa: E402

from loomcraft import ScriptedAgent, SessionStore, ToolBroker, parse_plan  # noqa: E402

DOC_A = """The Q3 migration completed on schedule.
Throughput improved by 18% after the cutover.
We did not observe any increase in error rates during the migration window.
The caching layer was the single largest contributor to the improvement.
Latency at the 99th percentile fell from 410ms to 260ms.
"""

DOC_B = """The Q3 migration slipped by two weeks against the original plan.
Throughput improved after the cutover, though by less than forecast.
We observed an increase in error rates during the migration window.
The caching layer contributed to the improvement but was not the largest factor.
Database connection pooling accounted for most of the latency reduction.
"""

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"


def banner(text: str) -> None:
    print(f"\n{BOLD}{text}{RESET}")
    print(f"{DIM}{'─' * 66}{RESET}")


def show(label: str, value: object) -> None:
    print(f"   {label:<32} {value}")


INPUT_REQUEST = {
    "title": "A second document is needed to compare",
    "message": (
        "You asked for a comparative brief, but only one document is available. "
        "Upload at least one more so claims can be checked against each other."
    ),
    "requirements": [
        {
            "key": "comparison_document",
            "label": "Comparison document",
            "description": "A second report covering the same period, to compare against.",
            "required": True,
            "min_files": 1,
            "max_files": 4,
            "allowed_extensions": [".txt", ".md"],
            "field_hints": ["migration outcome", "throughput", "error rates"],
        }
    ],
    "continue_prompt": "The comparison document is uploaded. Please continue.",
}

FULL_PLAN = {
    "goal": "Produce a comparative research brief across the uploaded reports.",
    "summary": "Extract, then summarise and theme in parallel, cross-check, and brief.",
    "revision": 1,
    "steps": [
        {"id": "extract", "title": "Extract document text", "kind": "capability", "capability": "docs.extract"},
        {"id": "summary", "title": "Summarise each document", "kind": "capability", "capability": "docs.summarise", "depends_on": ["extract"]},
        {"id": "themes", "title": "Identify shared themes", "kind": "capability", "capability": "docs.themes", "depends_on": ["extract"]},
        {"id": "conflicts", "title": "Cross-check for contradictions", "kind": "capability", "capability": "docs.contradictions", "depends_on": ["extract"]},
        {"id": "brief", "title": "Compose the brief", "kind": "capability", "capability": "docs.brief", "depends_on": ["summary", "themes", "conflicts"]},
        {"id": "verify", "title": "Verify the brief against sources", "kind": "review", "depends_on": ["brief"]},
        {"id": "answer", "title": "Answer the user", "kind": "answer", "depends_on": ["verify"]},
    ],
}


async def branch_user_uploads() -> None:
    """Happy path: the user supplies the missing document."""
    banner("BRANCH A — the user uploads the missing document")

    with TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "sessions")
        session = store.create("research-a")
        first = session.save_upload("q3-migration-engineering.md", DOC_A.encode())
        broker = ToolBroker(session, registry)

        # ── Turn 1: notice the gap, ask, stop ────────────────────────────────
        turn_one = ScriptedAgent(
            [
                ("session_context", {}),
                ("inspect_source", {"source_ref": first["source_ref"], "max_lines": 3}),
                ("capability_search", {"query": "compare documents contradictions"}),
                ("request_inputs", {"request": INPUT_REQUEST}),
                # Everything after the request is refused — proving the gate.
                ("publish_plan", {"plan": FULL_PLAN}),
            ],
            final_text="I need a second document before I can compare anything.",
        )
        result = await turn_one.run_turn(broker, "Compare our Q3 migration reports.")

        show("tool calls made", len(result.tool_calls))
        show("request published", result.tool_results[3].ok)
        request_id = result.tool_results[3].result["request"]["request_id"]
        show("request id", request_id)
        blocked = result.tool_results[4]
        show("planning while blocked", f"refused — {blocked.error_code}")
        show("agent said", result.text)

        # ── The user uploads and confirms ────────────────────────────────────
        banner("BRANCH A — the user responds")
        second = session.save_upload("q3-migration-product.md", DOC_B.encode())
        allocation = broker.fulfill_input_request(request_id)
        show("uploaded", second["filename"])
        show("allocated to slot", allocation["allocated"])
        show("broker unblocked", not broker.awaiting_inputs)

        # ── Turn 2: plan and execute ────────────────────────────────────────
        banner("BRANCH A — plan and execute")
        broker.begin_turn()
        published = await broker.dispatch("publish_plan", {"plan": FULL_PLAN})
        show("plan accepted", published.ok)
        for index, layer in enumerate(parse_plan(session.current_plan()).layers):
            note = "  ← concurrent" if len(layer) > 1 else ""
            show(f"layer {index}", ", ".join(layer) + note)

        documents = [first["source_ref"], second["source_ref"]]
        extracted = await broker.dispatch(
            "run_capability",
            {
                "capability_id": "docs.extract",
                "step_id": "extract",
                "inputs": {"documents": documents},
            },
        )
        corpus = extracted.result["artifacts"][0]["source_ref"]
        show("extract", extracted.result["status"])

        summary = await broker.dispatch(
            "run_capability",
            {"capability_id": "docs.summarise", "step_id": "summary", "inputs": {"corpus": corpus}},
        )
        themes = await broker.dispatch(
            "run_capability",
            {"capability_id": "docs.themes", "step_id": "themes", "inputs": {"corpus": corpus}},
        )
        conflicts = await broker.dispatch(
            "run_capability",
            {"capability_id": "docs.contradictions", "step_id": "conflicts", "inputs": {"corpus": corpus}},
        )
        show("summarise / themes / conflicts", "all succeeded")
        show("contradictions found", conflicts.result.get("status"))

        composed = await broker.dispatch(
            "run_capability",
            {
                "capability_id": "docs.brief",
                "step_id": "brief",
                "inputs": {
                    "summary": summary.result["artifacts"][0]["source_ref"],
                    "themes": themes.result["artifacts"][0]["source_ref"],
                    "contradictions": conflicts.result["artifacts"][0]["source_ref"],
                },
                "parameters": {"title": "Q3 migration: comparative brief"},
            },
        )
        show("brief", composed.result["status"])

        await broker.dispatch(
            "update_step",
            {"step_id": "verify", "status": "succeeded", "summary": "Claims traced to both sources."},
        )
        await broker.dispatch(
            "update_step",
            {"step_id": "answer", "status": "succeeded", "summary": "Brief delivered."},
        )
        show("plan complete", parse_plan(session.current_plan()).is_complete)

        brief_row = next(
            item for item in session.list_artifacts() if item["filename"] == "research-brief.md"
        )
        print(f"\n{DIM}{'─' * 66}{RESET}")
        print((session.root / brief_row["relpath"]).read_text())
        await broker.close()


async def branch_user_declines() -> None:
    """Failure path: the user declines, a step fails, the agent replans."""
    banner("BRANCH B — the user declines, a step fails, the agent replans")

    with TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "sessions")
        session = store.create("research-b")
        only = session.save_upload("q3-migration-engineering.md", DOC_A.encode())
        broker = ToolBroker(session, registry)
        broker.begin_turn()

        request = await broker.dispatch("request_inputs", {"request": INPUT_REQUEST})
        request_id = request.result["request"]["request_id"]
        broker.cancel_input_request(request_id)
        show("user declined", True)
        show("broker unblocked", not broker.awaiting_inputs)

        broker.begin_turn()
        await broker.dispatch("publish_plan", {"plan": FULL_PLAN})
        extracted = await broker.dispatch(
            "run_capability",
            {
                "capability_id": "docs.extract",
                "step_id": "extract",
                "inputs": {"documents": [only["source_ref"]]},
            },
        )
        corpus = extracted.result["artifacts"][0]["source_ref"]

        summary = await broker.dispatch(
            "run_capability",
            {"capability_id": "docs.summarise", "step_id": "summary", "inputs": {"corpus": corpus}},
        )
        themes = await broker.dispatch(
            "run_capability",
            {"capability_id": "docs.themes", "step_id": "themes", "inputs": {"corpus": corpus}},
        )

        banner("BRANCH B — the failure")
        failed = await broker.dispatch(
            "run_capability",
            {"capability_id": "docs.contradictions", "step_id": "conflicts", "inputs": {"corpus": corpus}},
        )
        show("conflicts", f"failed — {failed.error}")
        statuses = {step["id"]: step["status"] for step in session.current_plan()["steps"]}
        show("brief (downstream)", f"{statuses['brief']}  ← skipped automatically")
        show("verify / answer", f"{statuses['verify']} / {statuses['answer']}")

        banner("BRANCH B — replan with a reason")
        reduced = {
            "goal": FULL_PLAN["goal"],
            "summary": "Single-document brief: no cross-document comparison possible.",
            "revision": 2,
            "reason": (
                "Contradiction analysis needs at least two documents and the user "
                "declined to supply a second. Dropping the cross-check step and "
                "delivering a single-document brief instead."
            ),
            "steps": [
                {"id": "extract", "title": "Extract document text", "kind": "capability", "capability": "docs.extract"},
                {"id": "summary", "title": "Summarise the document", "kind": "capability", "capability": "docs.summarise", "depends_on": ["extract"]},
                {"id": "themes", "title": "Identify themes", "kind": "capability", "capability": "docs.themes", "depends_on": ["extract"]},
                {"id": "brief", "title": "Compose a single-source brief", "kind": "capability", "capability": "docs.brief", "depends_on": ["summary", "themes"]},
                {"id": "answer", "title": "Answer with the caveat", "kind": "answer", "depends_on": ["brief"]},
            ],
        }
        replanned = await broker.dispatch("publish_plan", {"plan": reduced})
        show("revision 2 accepted", replanned.ok)
        show("revisions retained", [item["revision"] for item in session.plan_history()])
        show("all steps reset", {
            step["id"]: step["status"] for step in session.current_plan()["steps"]
        })

        banner("BRANCH B — execute the reduced plan")
        # Prior artifacts survive a replan, so completed work is not redone —
        # the agent reuses the corpus it already extracted.
        show("reusing artifact", corpus)
        await broker.dispatch(
            "run_capability",
            {"capability_id": "docs.extract", "step_id": "extract", "inputs": {"documents": [only["source_ref"]]}},
        )
        await broker.dispatch(
            "run_capability",
            {"capability_id": "docs.summarise", "step_id": "summary", "inputs": {"corpus": corpus}},
        )
        await broker.dispatch(
            "run_capability",
            {"capability_id": "docs.themes", "step_id": "themes", "inputs": {"corpus": corpus}},
        )
        composed = await broker.dispatch(
            "run_capability",
            {
                "capability_id": "docs.brief",
                "step_id": "brief",
                # No contradictions input this time — the optional slot is simply
                # absent, and the capability's variant contract still validates.
                "inputs": {
                    "summary": summary.result["artifacts"][0]["source_ref"],
                    "themes": themes.result["artifacts"][0]["source_ref"],
                },
                "parameters": {"title": "Q3 migration: single-source brief"},
            },
        )
        show("brief", composed.result["status"])
        show("contradiction section", "omitted, with an explicit caveat")

        await broker.dispatch(
            "update_step",
            {
                "step_id": "answer",
                "status": "succeeded",
                "summary": "Delivered a single-source brief and explained the limitation.",
            },
        )
        show("plan complete", parse_plan(session.current_plan()).is_complete)
        show("events recorded", session.events.last_seq)
        show("hash chain intact", session.events.verify())

        brief_row = [
            item for item in session.list_artifacts() if item["filename"] == "research-brief.md"
        ][-1]
        print(f"\n{DIM}{'─' * 66}{RESET}")
        print((session.root / brief_row["relpath"]).read_text())
        await broker.close()


async def main() -> int:
    await branch_user_uploads()
    await branch_user_declines()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
