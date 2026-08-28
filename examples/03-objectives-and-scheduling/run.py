#!/usr/bin/env python3
"""Example 3 — declared objectives, whole-plan scheduling, and a Codex-style host.

    python examples/03-objectives-and-scheduling/run.py

No model and no API key. Every call still goes through the real broker, engine
and event log.

A breeder asks two questions about one field trial. One of them is answerable
from the data; the other is not, and the interesting behaviour is what the
system does about that.

What it demonstrates:

1.  Objectives declared before the work, and an evidence ledger that must
    account for each of them.
2.  `executed` coverage refused without a step or artifact to point at.
3.  `execute_plan` — the whole graph in one scheduled run, with genuine
    concurrency, per-step retry, and a tolerated failure.
4.  `on_failure: "continue"` — a branch that cannot be estimated does not take
    the report down with it.
5.  A `review` step bound to a review-scoped capability, so the verdict is
    server-owned rather than self-reported.
6.  A revision that may reclassify an objective but not drop it.
7.  The same broker driven over JSON-RPC, the way a Codex app-server would.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "packages" / "core" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from capabilities import registry  # noqa: E402

from loomcraft import (  # noqa: E402
    AppServerBridge,
    SessionStore,
    ToolBroker,
    parse_plan,
)

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"

TRIAL = """plot\tgenotype\tyield
1\tG-ALPHA\t6.2
2\tG-ALPHA\t6.9
3\tG-BETA\t5.1
4\tG-BETA\t5.4
5\tG-GAMMA\t7.8
6\tG-GAMMA\t7.1
7\tG-DELTA\tNA
8\tG-DELTA\t4.9
"""


def banner(text: str) -> None:
    print(f"\n{BOLD}{text}{RESET}")
    print(f"{DIM}{'─' * 74}{RESET}")


def show(label: str, value: object) -> None:
    print(f"   {label:<34} {value}")


def plan_v1(source_ref: str) -> dict:
    """Two questions, four steps, one branch that will not work out."""
    return {
        "goal": "Which genotypes yield best, and is there a maternal effect?",
        "summary": "Clean, then scan yield and stability together, annotate, review.",
        "revision": 1,
        "analysis_profile": "field-trial",
        "objectives": [
            {
                "id": "q1",
                "question": "Which genotypes have the highest yield effect?",
                "estimand": "per-genotype deviation from the trial mean",
                "independent_unit": "plot",
                "validation_requirements": ["effects plausible for this trial"],
            },
            {
                "id": "q2",
                "question": "Is there a maternal component to yield?",
                "estimand": "maternal variance share",
                "independent_unit": "dam",
            },
        ],
        "analysis_coverage": [
            {
                "objective_id": "q1",
                "status": "planned",
                "reason": "yield scan followed by a calibration review",
                "step_ids": ["scan", "check"],
            },
            {
                "objective_id": "q2",
                "status": "planned",
                "reason": "maternal partition planned; identifiability unverified",
                "step_ids": ["maternal"],
            },
        ],
        "steps": [
            {
                "id": "clean",
                "title": "Clean the trial table",
                "kind": "capability",
                "capability": "trial.clean",
            },
            {
                "id": "scan",
                "title": "Scan genotypes for a yield effect",
                "kind": "capability",
                "capability": "trial.yield_scan",
                "depends_on": ["clean"],
            },
            {
                "id": "spread",
                "title": "Score genotype stability",
                "kind": "capability",
                "capability": "trial.stability",
                "depends_on": ["clean"],
            },
            {
                "id": "maternal",
                "title": "Estimate the maternal component",
                "kind": "capability",
                "capability": "trial.maternal_scan",
                "depends_on": ["clean"],
                # This branch may come back empty without ending the run.
                "on_failure": "continue",
            },
            {
                "id": "annotate",
                "title": "Annotate against the variety register",
                "kind": "capability",
                "capability": "trial.annotate",
                "depends_on": ["scan"],
                "retry": {"max_attempts": 3, "backoff_seconds": 0.01},
                "timeout_seconds": 30,
            },
            {
                "id": "check",
                "title": "Check the effects are plausible",
                # A review the server owns, because it binds a review capability.
                "kind": "review",
                "capability": "review.calibration",
                "depends_on": ["annotate"],
            },
        ],
        "metadata": {"source": source_ref},
    }


async def main() -> None:
    with TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "sessions")
        session = store.create("field-trial")
        upload = session.save_upload("trial.tsv", TRIAL.encode("utf-8"))
        broker = ToolBroker(session, registry)
        broker.begin_turn()

        banner("1 · The ledger refuses a claim with no evidence behind it")
        bad = plan_v1(upload["source_ref"])
        bad["analysis_coverage"][0] = {
            "objective_id": "q1",
            "status": "executed",
            "reason": "we looked at it",
        }
        response = await broker.dispatch("publish_plan", {"plan": bad})
        show("publish 'executed' with no proof", "refused" if not response.ok else "ACCEPTED")
        show("why", response.error)

        banner("2 · Publish the real plan")
        response = await broker.dispatch("publish_plan", {"plan": plan_v1(upload["source_ref"])})
        show("published", response.ok)
        parsed = parse_plan(session.current_plan())
        show("objectives declared", [item.id for item in parsed.objectives])
        show("dependency layers", parsed.layers)
        print(f"{DIM}   scan / spread / maternal share a layer — no edge between them,{RESET}")
        print(f"{DIM}   so the scheduler will dispatch all three at once.{RESET}")

        banner("3 · Run the whole plan in one scheduled execution")
        response = await broker.dispatch(
            "execute_plan",
            {"inputs": {"clean": {"inputs": {"trial": upload["source_ref"]}}}},
        )
        execution = response.result
        show("tool reported ok", response.ok)
        show("run status", execution["status"])
        show("nodes in one run", len(execution["nodes"]))

        print()
        for step in session.current_plan()["steps"]:
            attempts = step.get("attempts", 0)
            marker = "·" if step["status"] == "succeeded" else "×"
            print(
                f"   {marker} {step['id']:<10} {step['status']:<10} "
                f"attempts={attempts}  {DIM}{(step.get('summary') or '')[:38]}{RESET}"
            )

        banner("4 · A tolerated failure is reported, not hidden")
        tolerated = [row for row in execution["failed_nodes"] if row["tolerated"]]
        blocking = [row for row in execution["failed_nodes"] if not row["tolerated"]]
        show("failed nodes", len(execution["failed_nodes"]))
        show("…of which tolerated", [row["node_id"] for row in tolerated])
        show("…of which blocking", [row["node_id"] for row in blocking] or "none")
        show("run still succeeded", execution["status"] == "succeeded")
        print(f"{DIM}   'maternal' failed under on_failure: continue, so the run finished{RESET}")
        print(f"{DIM}   and its error is still on the step and in failed_nodes.{RESET}")

        banner("5 · Retry and the server-owned review")
        show("annotate attempts", session.current_plan()["steps"][4]["attempts"])
        show("check (review) status", session.current_plan()["steps"][5]["status"])
        refused = await broker.dispatch(
            "update_step", {"step_id": "check", "status": "succeeded"}
        )
        show("agent self-reporting 'check'", "refused" if not refused.ok else "ACCEPTED")
        show("why", refused.error)

        banner("6 · A revision may reclassify a question, not drop it")
        dropped = plan_v1(upload["source_ref"])
        dropped["revision"] = 2
        dropped["reason"] = "narrowing to what worked"
        dropped["objectives"] = [dropped["objectives"][0]]
        dropped["analysis_coverage"] = [dropped["analysis_coverage"][0]]
        response = await broker.dispatch("publish_plan", {"plan": dropped})
        show("dropping q2 entirely", "refused" if not response.ok else "ACCEPTED")
        show("why", (response.error or "")[:88])

        honest = plan_v1(upload["source_ref"])
        honest["revision"] = 2
        honest["reason"] = (
            "the maternal component is not identifiable from a trial table with "
            "no dam column"
        )
        honest["analysis_coverage"] = [
            {
                "objective_id": "q1",
                "status": "executed",
                "reason": "yield scan reviewed and found plausible",
                "step_ids": ["scan", "check"],
            },
            {
                "objective_id": "q2",
                "status": "not_estimable",
                "reason": "the trial table has no dam column",
                "next_action": "request a pedigree export including dam ids",
            },
        ]
        response = await broker.dispatch("publish_plan", {"plan": honest})
        show("reclassifying q2", "accepted" if response.ok else response.error)

        parsed = parse_plan(session.current_plan())
        print()
        for item in parsed.analysis_coverage:
            print(f"   {item.objective_id}  {item.status:<14} {DIM}{item.reason[:44]}{RESET}")
            if item.next_action:
                print(f"      {BOLD}next:{RESET} {item.next_action}")

        banner("7 · The same broker, driven the way Codex would drive it")
        bridge = AppServerBridge(broker)
        handshake = await bridge.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        )
        listing = await bridge.handle(
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
        )
        called = await bridge.handle(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "item/tool/call",
                "params": {"name": "session_context", "arguments": "{}"},
            }
        )
        show("protocol", handshake["result"]["protocolVersion"])
        show("tools advertised", len(listing["result"]["tools"]))
        show("item/tool/call ok", not called["result"]["isError"])
        context = called["result"]["structuredContent"]["result"]
        show("plan revision it sees", context["plan"]["revision"])

        denied = await bridge.handle(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "params": None,
                    "name": "run_capability",
                    "arguments": {"capability_id": "trial.clean", "step_id": "clean"},
                },
            }
        )
        show("re-running a finished step", "refused" if denied["result"]["isError"] else "ACCEPTED")
        print(f"{DIM}   Same authorization as an in-process loop. The transport is{RESET}")
        print(f"{DIM}   not a second door into the engine.{RESET}")

        banner("8 · The audit trail")
        events = session.events.read()
        show("events recorded", len(events))
        show("hash chain verifies", session.events.verify())
        show("artifacts", [item["filename"] for item in session.list_artifacts()])
        show("plan revisions retained", [p["revision"] for p in session.plan_history()])

        print(f"\n{DIM}{'─' * 74}{RESET}")
        print(
            f"{BOLD}Two questions asked. One answered with evidence, one recorded as{RESET}\n"
            f"{BOLD}unanswerable with the reason and what would change it.{RESET}\n"
            f"{DIM}Neither outcome was narrated — both were enforced.{RESET}\n"
        )
        await broker.close()


if __name__ == "__main__":
    asyncio.run(main())
