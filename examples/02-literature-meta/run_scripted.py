#!/usr/bin/env python3
"""Example 2 — a meta-analysis that has to ask for what it is missing.

    python examples/02-literature-meta/run_scripted.py

No model and no API key. Both branches run so you can see each one end to end.

The user uploads two trial reports and asks whether a salt-tolerance treatment
works. Two studies is not enough to pool: between-study variance estimated from
one degree of freedom is not an estimate, so ``lit.meta`` refuses. What the agent
does about that is the example.

**Branch A** — the agent notices the gap *before* planning, calls
``request_inputs``, and stops. Everything it tries after that is refused until
the request is fulfilled. The user uploads the other three reports, the broker
allocates them to the typed slot, and the full analysis runs: pooling, influence
and funnel asymmetry all fan out from one parent and then fan back in.

The finding is not the pooled number. Pooled across five studies the treatment
looks worth +8.5%, and the leave-one-out analysis shows that the entire excess is
one small greenhouse study — drop it and the effect falls to +5.4% while the
heterogeneity goes from I² = 74% to exactly zero. Egger's regression agrees from
a different direction. None of that is written into a fixture; it is computed by
``capabilities.py`` from the numbers in ``studies.py``.

**Branch B** — the user declines. ``lit.meta`` fails for real, its dependents are
skipped, and the agent publishes revision 2 with a reason: a narrative synthesis
over two studies, honestly labelled as not a meta-analysis.

What it demonstrates:

1.  Structured file requests, and the execution gate they impose.
2.  Upload allocation across a typed slot.
3.  A three-way parallel layer and a fan-in.
4.  Optional inputs — one capability, two legitimate shapes of output.
5.  A genuine step failure with a statistical reason.
6.  Skip propagation to the whole downstream subtree.
7.  Replan discipline — an increasing revision carrying a reason.
8.  Artifact reuse across the replan.
9.  Agent-reported `review` / `answer` steps.
10. A hash-chained audit log over both branches.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[2] / "packages" / "core" / "src"))

import studies  # noqa: E402
from capabilities import registry  # noqa: E402

from loomcraft import ScriptedAgent, SessionStore, ToolBroker, parse_plan  # noqa: E402

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"


def banner(text: str) -> None:
    print(f"\n{BOLD}{text}{RESET}")
    print(f"{DIM}{'─' * 70}{RESET}")


def show(label: str, value: object) -> None:
    print(f"   {label:<32} {value}")


def artifact_ref(result: dict, port: str) -> str:
    for item in result["artifacts"]:
        if item.get("port_name") == port:
            return item["source_ref"]
    raise KeyError(f"no artifact on port {port!r}")


def read_json(session, source_ref: str) -> dict:
    return json.loads(session.resolve_source(source_ref).path.read_text())


INPUT_REQUEST = {
    "title": "More trials are needed before anything can be pooled",
    "message": (
        "Two studies cannot support a meta-analysis: between-study variance "
        "estimated from one degree of freedom is not an estimate. Upload the "
        "remaining trial reports and the pooled analysis can run."
    ),
    "requirements": [
        {
            "key": "additional_trials",
            "label": "Additional trial reports",
            "description": "Further trials reporting the same outcome, with effect and 95% CI.",
            "required": True,
            "min_files": 1,
            "max_files": 6,
            "allowed_extensions": [".txt", ".md"],
            "field_hints": ["sample size", "effect estimate", "confidence interval"],
        }
    ],
    "continue_prompt": "The additional trials are uploaded. Please continue.",
}

FULL_PLAN = {
    "goal": "Does the salt-tolerance treatment improve grain yield, and by how much?",
    "summary": "Extract and harmonise, then pool, probe influence and test for funnel asymmetry in parallel.",
    "revision": 1,
    "steps": [
        {"id": "extract", "title": "Extract study records", "kind": "capability", "capability": "lit.extract"},
        {"id": "harmonise", "title": "Harmonise effect sizes", "kind": "capability", "capability": "lit.harmonise", "depends_on": ["extract"]},
        {"id": "meta", "title": "Pool the effects", "kind": "capability", "capability": "lit.meta", "depends_on": ["harmonise"]},
        {"id": "influence", "title": "Leave-one-out influence", "kind": "capability", "capability": "lit.influence", "depends_on": ["harmonise"]},
        {"id": "bias", "title": "Test for funnel asymmetry", "kind": "capability", "capability": "lit.bias", "depends_on": ["harmonise"]},
        {"id": "brief", "title": "Compose the evidence brief", "kind": "capability", "capability": "lit.brief", "depends_on": ["meta", "influence", "bias"]},
        {"id": "verify", "title": "Check the brief against the studies", "kind": "review", "depends_on": ["brief"]},
        {"id": "answer", "title": "Answer the user", "kind": "answer", "depends_on": ["verify"]},
    ],
}


async def branch_user_uploads() -> None:
    """Happy path: the user supplies the missing trials."""
    banner("BRANCH A — the agent asks before it plans")

    with TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "sessions")
        session = store.create("meta-a")
        for name in studies.STARTING:
            session.save_upload(name, studies.REPORTS[name].encode())
        first = session.list_uploads()[0]
        broker = ToolBroker(session, registry)

        # ── Turn 1: notice the gap, ask, stop ────────────────────────────────
        turn_one = ScriptedAgent(
            [
                ("session_context", {}),
                ("inspect_source", {"source_ref": first["source_ref"], "max_lines": 5}),
                ("capability_search", {"query": "pool effect sizes across trials"}),
                ("request_inputs", {"request": INPUT_REQUEST}),
                # Everything after the request is refused — proving the gate.
                ("publish_plan", {"plan": FULL_PLAN}),
            ],
            final_text="Two trials cannot be pooled. I need the rest before I can answer this.",
        )
        result = await turn_one.run_turn(
            broker, "Does the salt-tolerance treatment actually improve yield?"
        )

        show("studies on hand", len(studies.STARTING))
        show("request published", result.tool_results[3].ok)
        request_id = result.tool_results[3].result["request"]["request_id"]
        blocked = result.tool_results[4]
        show("planning while blocked", f"refused — {blocked.error_code}")
        show("agent said", result.text)

        # ── The user uploads and confirms ────────────────────────────────────
        banner("BRANCH A — the user responds")
        for name in studies.REQUESTED:
            session.save_upload(name, studies.REPORTS[name].encode())
        allocation = broker.fulfill_input_request(request_id)
        show("uploaded", ", ".join(studies.REQUESTED))
        show("allocated to slot", allocation["allocated"])
        show("broker unblocked", not broker.awaiting_inputs)

        # ── Turn 2: plan and execute ────────────────────────────────────────
        banner("BRANCH A — plan and execute")
        broker.begin_turn()
        published = await broker.dispatch("publish_plan", {"plan": FULL_PLAN})
        show("plan accepted", published.ok)
        for index, layer in enumerate(parse_plan(session.current_plan()).layers):
            note = "   ← same dependency, so the engine may run all three at once" if len(layer) > 2 else ""
            show(f"layer {index}", ", ".join(layer) + note)

        refs = [item["source_ref"] for item in session.list_uploads()]
        extracted = await broker.dispatch(
            "run_capability",
            {"capability_id": "lit.extract", "step_id": "extract", "inputs": {"reports": refs}},
        )
        corpus_ref = artifact_ref(extracted.result, "corpus")
        corpus = read_json(session, corpus_ref)
        show("studies extracted", len(corpus["studies"]))
        show("unparsed", len(corpus["unparsed"]) or "none")

        harmonised = await broker.dispatch(
            "run_capability",
            {"capability_id": "lit.harmonise", "step_id": "harmonise", "inputs": {"corpus": corpus_ref}},
        )
        harmonised_ref = artifact_ref(harmonised.result, "harmonised")
        rows = read_json(session, harmonised_ref)["studies"]
        show("outcome agreed across all", read_json(session, harmonised_ref)["outcome"])
        show("standard errors recovered", ", ".join(f"{row['se']:.2f}" for row in rows))

        pooled_result = await broker.dispatch(
            "run_capability",
            {"capability_id": "lit.meta", "step_id": "meta", "inputs": {"harmonised": harmonised_ref}},
        )
        pooled_ref = artifact_ref(pooled_result.result, "pooled")
        pooled = read_json(session, pooled_ref)
        show("pooled effect", f"{pooled['estimate']:+.2f}%  "
                              f"(95% CI {pooled['ci_low']:+.2f} to {pooled['ci_high']:+.2f})")
        show("heterogeneity", f"Q = {pooled['q']} on {pooled['df']} df → I² = {pooled['i_squared']}%")

        influence_result = await broker.dispatch(
            "run_capability",
            {"capability_id": "lit.influence", "step_id": "influence", "inputs": {"harmonised": harmonised_ref}},
        )
        influence_ref = artifact_ref(influence_result.result, "influence")
        influence = read_json(session, influence_ref)
        top = influence["most_influential"]

        bias_result = await broker.dispatch(
            "run_capability",
            {"capability_id": "lit.bias", "step_id": "bias", "inputs": {"harmonised": harmonised_ref}},
        )
        bias_ref = artifact_ref(bias_result.result, "bias")
        bias = read_json(session, bias_ref)

        # ── The finding ──────────────────────────────────────────────────────
        banner("BRANCH A — what the analysis actually found")
        for row in influence["leave_one_out"]:
            marker = "  ←" if row["omitted"] == top["omitted"] else ""
            show(f"without {row['omitted']}",
                 f"{row['estimate_without']:+.2f}%   I² = {row['i_squared_without']:>5.1f}%{marker}")
        show("most influential", top["omitted"])
        show("it moves the estimate by", f"{top['estimate_shift']:+.2f} points")
        show("and the heterogeneity by", f"{top['i_squared_shift']:+.1f} points")
        show("expected outlier", studies.EXPECTED_OUTLIER)
        show("agrees?", top["omitted"] == studies.EXPECTED_OUTLIER)
        show("Egger's intercept", f"{bias['intercept']:+.2f} (t = {bias['t']}) — {bias['reading']}")

        brief_result = await broker.dispatch(
            "run_capability",
            {
                "capability_id": "lit.brief", "step_id": "brief",
                "inputs": {
                    "harmonised": harmonised_ref,
                    "pooled": pooled_ref,
                    "influence": influence_ref,
                    "bias": bias_ref,
                },
            },
        )
        show("brief sections", "corpus + pooled + influence + bias")

        await broker.dispatch(
            "update_step",
            {"step_id": "verify", "status": "succeeded",
             "summary": f"Every figure in the brief traces to an artifact. "
                        f"{top['omitted']} flagged as driving the pooled estimate."},
        )
        await broker.dispatch(
            "update_step",
            {"step_id": "answer", "status": "succeeded",
             "summary": "Positive but smaller than the headline; one small study inflates it."},
        )
        show("plan progress", parse_plan(session.current_plan()).progress)
        show("events", session.events.last_seq)
        show("hash chain intact", session.events.verify())

        brief_row = next(
            item for item in session.list_artifacts() if item["filename"].endswith(".md")
        )
        print(f"\n{DIM}{'─' * 70}{RESET}")
        print((session.root / brief_row["relpath"]).read_text())

        await broker.close()


async def branch_user_declines() -> None:
    """The user declines, a step fails for a real reason, and the agent replans."""
    banner("BRANCH B — the user declines, a step fails, the agent replans")

    with TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "sessions")
        session = store.create("meta-b")
        for name in studies.STARTING:
            session.save_upload(name, studies.REPORTS[name].encode())
        broker = ToolBroker(session, registry)
        broker.begin_turn()

        request = await broker.dispatch("request_inputs", {"request": INPUT_REQUEST})
        request_id = request.result["request"]["request_id"]
        broker.cancel_input_request(request_id)
        show("request cancelled", True)
        show("broker unblocked", not broker.awaiting_inputs)

        broker.begin_turn()
        await broker.dispatch("publish_plan", {"plan": FULL_PLAN})
        show("plan published anyway", "the agent does not yet know pooling will fail")

        refs = [item["source_ref"] for item in session.list_uploads()]
        extracted = await broker.dispatch(
            "run_capability",
            {"capability_id": "lit.extract", "step_id": "extract", "inputs": {"reports": refs}},
        )
        corpus_ref = artifact_ref(extracted.result, "corpus")
        harmonised = await broker.dispatch(
            "run_capability",
            {"capability_id": "lit.harmonise", "step_id": "harmonise", "inputs": {"corpus": corpus_ref}},
        )
        harmonised_ref = artifact_ref(harmonised.result, "harmonised")
        show("extracted + harmonised", f"{len(read_json(session, harmonised_ref)['studies'])} studies")

        # ── The failure ──────────────────────────────────────────────────────
        banner("BRANCH B — the failure, and what it takes down with it")
        failed = await broker.dispatch(
            "run_capability",
            {"capability_id": "lit.meta", "step_id": "meta", "inputs": {"harmonised": harmonised_ref}},
        )
        statuses = {step["id"]: step["status"] for step in session.current_plan()["steps"]}
        show("meta", f"{statuses['meta']} — {failed.error}")
        for key in ("brief", "verify", "answer"):
            show(key, f"{statuses[key]}  ← skipped, never ran")
        show("influence / bias", f"{statuses['influence']} / {statuses['bias']}  "
                                 "← untouched: they never depended on meta")

        # ── Replan ───────────────────────────────────────────────────────────
        banner("BRANCH B — replan with a reason")
        narrative_plan = {
            "goal": FULL_PLAN["goal"],
            "summary": "Two studies only: synthesise narratively and say so plainly.",
            "revision": 2,
            "reason": (
                "Pooling needs at least three studies to estimate between-study "
                "variance and the user declined to supply more. Dropping the "
                "pooled, influence and bias steps and reporting a narrative "
                "synthesis that does not claim to be a meta-analysis."
            ),
            "steps": [
                {"id": "extract", "title": "Extract study records", "kind": "capability", "capability": "lit.extract"},
                {"id": "harmonise", "title": "Harmonise effect sizes", "kind": "capability", "capability": "lit.harmonise", "depends_on": ["extract"]},
                {"id": "brief", "title": "Compose a narrative synthesis", "kind": "capability", "capability": "lit.brief", "depends_on": ["harmonise"]},
                {"id": "verify", "title": "Check the synthesis", "kind": "review", "depends_on": ["brief"]},
                {"id": "answer", "title": "Answer the user", "kind": "answer", "depends_on": ["verify"]},
            ],
        }
        show("revision 2 accepted", (await broker.dispatch("publish_plan", {"plan": narrative_plan})).ok)
        show("history retained", [item["revision"] for item in session.plan_history()])

        # ── Execute the reduced plan, reusing what already succeeded ─────────
        banner("BRANCH B — execute the reduced plan")
        # Two kinds of state, deliberately separate. Publishing a revision resets
        # every *step* to pending — including the two that had already succeeded
        # — because a new plan is a new set of claims about what has been done.
        # It does not touch *artifacts*: those belong to the session, and the
        # revision-1 output is still resolvable and still passes its checksum.
        statuses = {step["id"]: step["status"] for step in session.current_plan()["steps"]}
        show("step state after replan", f"harmonise is {statuses['harmonise']} again")
        show("revision 1 artifact", f"still resolvable — "
                                    f"{len(read_json(session, harmonised_ref)['studies'])} studies")

        early = await broker.dispatch(
            "run_capability",
            {"capability_id": "lit.brief", "step_id": "brief",
             "inputs": {"harmonised": harmonised_ref}},
        )
        show("briefing before re-running", f"refused — {early.error}")

        # So the cheap upstream steps run again. They are deterministic, so this
        # costs a few milliseconds and buys a plan whose recorded state is true.
        corpus_ref = artifact_ref((await broker.dispatch(
            "run_capability",
            {"capability_id": "lit.extract", "step_id": "extract", "inputs": {"reports": refs}},
        )).result, "corpus")
        harmonised_ref = artifact_ref((await broker.dispatch(
            "run_capability",
            {"capability_id": "lit.harmonise", "step_id": "harmonise", "inputs": {"corpus": corpus_ref}},
        )).result, "harmonised")

        # `lit.brief` declares pooled/influence/bias as optional, so the same
        # capability produces a legitimately different document here.
        reduced = await broker.dispatch(
            "run_capability",
            {"capability_id": "lit.brief", "step_id": "brief",
             "inputs": {"harmonised": harmonised_ref}},
        )
        show("brief", reduced.result["status"])
        show("optional inputs supplied", "none — and the capability says so in the output")

        await broker.dispatch(
            "update_step",
            {"step_id": "verify", "status": "succeeded",
             "summary": "The synthesis states plainly that no pooled effect was estimated."},
        )
        await broker.dispatch(
            "update_step",
            {"step_id": "answer", "status": "succeeded",
             "summary": "Two studies, both positive, no defensible pooled estimate."},
        )
        show("plan progress", parse_plan(session.current_plan()).progress)
        show("events", session.events.last_seq)
        show("hash chain intact", session.events.verify())

        brief_row = [
            item for item in session.list_artifacts() if item["filename"].endswith(".md")
        ][-1]
        print(f"\n{DIM}{'─' * 70}{RESET}")
        print((session.root / brief_row["relpath"]).read_text())

        await broker.close()


async def main() -> int:
    await branch_user_uploads()
    await branch_user_declines()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
