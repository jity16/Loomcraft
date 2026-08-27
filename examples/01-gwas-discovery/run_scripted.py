#!/usr/bin/env python3
"""Example 1 — an association study that discovers its own first answer was wrong.

    python examples/01-gwas-discovery/run_scripted.py

No model and no API key. The tool calls a real agent would make are issued
directly, but every one still goes through the broker, so this exercises the
same validation, scheduling, retry, approval and event path as a live run —
which is why it doubles as a CI smoke test.

The scientific arc is the point. A naive per-marker scan on a structured cohort
produces a genomic inflation factor near 2.8 and an FDR list that is mostly
false positives. Nothing crashes; the numbers just are not what they look like.
A `review` step reads lambda, the agent concludes the model is confounded, and
publishes revision 2 with a kinship-corrected mixed model. Lambda collapses to
about 0.97 and exactly the three markers with a real effect survive.

None of that is narrated. `data.py` builds a cohort where ancestry moves both
the phenotype and most allele frequencies, and the inflation is the honest
statistical consequence.

What it demonstrates, in order:

1.  Discovery — context, capability search, bounded file preview.
2.  DAG validation and layering — the plan is checked before anything runs.
3.  Dependency gating — a step cannot jump ahead of its dependencies.
4.  Typed contracts and input variants — a real PLINK triple or a real VCF.
5.  Capability execution with typed, port-addressed artifacts.
6.  The naive scan, and the diagnostic that condemns it.
7.  Agent-reported `review`, and three guardrails that force a replan rather
    than let the agent paper over the problem in place.
8.  Replan discipline — revisions must increase and must explain themselves.
9.  Artifact reuse across the replan, and the other input variant.
10. Two branches off one parent — the shape that makes parallelism available.
11. The corrected scan: lambda collapses and the real markers survive.
12. Retry with exponential backoff, then fan-in from three artifacts.
13. **Real engine parallelism** via a registered workflow.
14. **Human approval** — registering a finding parks until someone confirms.
15. Failure and skip propagation.
16. The audit trail.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[2] / "packages" / "core" / "src"))

import data  # noqa: E402
from capabilities import registry, reset_annotation_attempts  # noqa: E402

from loomcraft import (  # noqa: E402
    Engine,
    SessionStore,
    ToolBroker,
    graph_from_capability,
    graph_from_workflow,
    parse_plan,
)

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"


def banner(number: int, text: str) -> None:
    print(f"\n{BOLD}{number:>2} · {text}{RESET}")
    print(f"{DIM}{'─' * 68}{RESET}")


def show(label: str, value: object) -> None:
    print(f"   {label:<32} {value}")


def artifact_ref(result: dict, port: str) -> str:
    """Source ref of the artifact a runner emitted on a named port.

    Selecting by port rather than by position is the point of declaring outputs:
    ``gwas.qc`` emits two artifacts and a caller that indexed into the list would
    silently swap them the day the runner emits them in the other order.
    """
    for item in result["artifacts"]:
        if item.get("port_name") == port:
            return item["source_ref"]
    ports = [item.get("port_name") for item in result["artifacts"]]
    raise KeyError(f"no artifact on port {port!r}; got {ports}")


def read_json(session: object, source_ref: str) -> dict:
    """Read an artifact back the way the agent would — through the session.

    ``resolve_source`` re-checks containment and re-verifies the recorded
    checksum on every call, so this is not a shortcut around the trust model.
    """
    return json.loads(session.resolve_source(source_ref).path.read_text())


async def main() -> int:
    reset_annotation_attempts()
    cohort = data.build_cohort()

    with TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "sessions")
        session = store.create("gwas-discovery-demo")

        # The same cohort in both accepted shapes, so the input-variant contract
        # has something real to choose between.
        bed = session.save_upload("cohort.bed", data.as_matrix(cohort).encode())
        bim = session.save_upload("cohort.bim", data.as_bim(cohort).encode())
        fam = session.save_upload("cohort.fam", data.as_fam(cohort).encode())
        vcf = session.save_upload("cohort.vcf", data.as_matrix(cohort).encode())

        broker = ToolBroker(session, registry)
        broker.begin_turn()

        # Used in a few places below to reach the engine directly, without the
        # plan-step bookkeeping the broker adds. Everything an agent does goes
        # through the broker; this is the example looking underneath it.
        engine = Engine(registry, session, emit=lambda *_: None)

        # ── 1 ────────────────────────────────────────────────────────────────
        banner(1, "Discovery: what the agent can see")
        context = await broker.dispatch("session_context", {})
        show("uploads visible", len(context.result["uploads"]))
        show("capabilities registered", context.result["catalog"]["capability_count"])
        show("workflows registered", context.result["catalog"]["workflow_count"])

        found = await broker.dispatch(
            "capability_search", {"query": "test markers for association with phenotype"}
        )
        show("best capability match", found.result["results"][0]["id"])

        preview = await broker.dispatch(
            "inspect_source", {"source_ref": bed["source_ref"], "max_lines": 1}
        )
        header = preview.result["preview_lines"][0].split("\t")
        show("cohort columns", f"{header[0]}, {header[1]}, {header[2]}, {len(header) - 3} markers")
        show("samples", len(cohort["samples"]))

        # ── 2 ────────────────────────────────────────────────────────────────
        banner(2, "Publish revision 1: the obvious plan")
        naive_plan = {
            "goal": "Find markers associated with salt tolerance in the uploaded cohort.",
            "summary": "QC the genotypes, scan every marker, correct for multiple testing.",
            "revision": 1,
            "steps": [
                {"id": "qc", "title": "Quality control", "kind": "capability", "capability": "gwas.qc"},
                {"id": "assoc", "title": "Association scan", "kind": "capability", "capability": "gwas.associate", "depends_on": ["qc"]},
                {"id": "correct", "title": "Correct for multiple testing", "kind": "capability", "capability": "gwas.correct", "depends_on": ["assoc"]},
                {"id": "review", "title": "Check the model is calibrated", "kind": "review", "depends_on": ["correct"]},
                {"id": "answer", "title": "Report the associated loci", "kind": "answer", "depends_on": ["review"]},
            ],
        }
        show("accepted", (await broker.dispatch("publish_plan", {"plan": naive_plan})).ok)
        for index, layer in enumerate(parse_plan(session.current_plan()).layers):
            show(f"layer {index}", ", ".join(layer))

        cyclic = await broker.dispatch(
            "publish_plan",
            {
                "plan": {
                    "goal": "g", "revision": 9, "reason": "test",
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
            {"capability_id": "gwas.associate", "step_id": "assoc", "inputs": {"cohort": bed["source_ref"]}},
        )
        show("refused", not early.ok)
        show("reason", early.error)

        # ── 4 ────────────────────────────────────────────────────────────────
        banner(4, "Typed contracts: a real triple or a real VCF, never half of each")
        half = await broker.dispatch(
            "run_capability",
            {"capability_id": "gwas.qc", "step_id": "qc", "inputs": {"bed": bed["source_ref"]}},
        )
        show(".bed with no .bim/.fam", half.error)

        mixed = await broker.dispatch(
            "run_capability",
            {
                "capability_id": "gwas.qc", "step_id": "qc",
                "inputs": {"bed": bed["source_ref"], "bim": bim["source_ref"], "vcf": vcf["source_ref"]},
            },
        )
        show("a triple plus a VCF", mixed.error)

        bad_param = await broker.dispatch(
            "run_capability",
            {
                "capability_id": "gwas.qc", "step_id": "qc",
                "inputs": {"bed": bed["source_ref"], "bim": bim["source_ref"], "fam": fam["source_ref"]},
                "parameters": {"min_maf": 0.9},
            },
        )
        show("MAF above 0.5", bad_param.error)

        # ── 5 ────────────────────────────────────────────────────────────────
        banner(5, "Execute: quality control")
        qc = await broker.dispatch(
            "run_capability",
            {
                "capability_id": "gwas.qc", "step_id": "qc",
                "inputs": {"bed": bed["source_ref"], "bim": bim["source_ref"], "fam": fam["source_ref"]},
                "parameters": {"min_maf": 0.05, "min_call_rate": 0.9},
            },
        )
        cohort_ref = artifact_ref(qc.result, "cohort")
        qc_report_ref = artifact_ref(qc.result, "qc_report")
        qc_report = read_json(session, qc_report_ref)
        show("status", qc.result["status"])
        show("markers", f"{qc_report['markers_in']} → {qc_report['markers_out']}")
        show("dropped", ", ".join(
            f"{row['marker']} ({row['reason']})" for row in qc_report["dropped_markers"]
        ) or "none")
        show("samples", f"{qc_report['samples_in']} → {qc_report['samples_out']}")

        # ── 6 ────────────────────────────────────────────────────────────────
        banner(6, "The naive scan, and the diagnostic that condemns it")
        naive = await broker.dispatch(
            "run_capability",
            {
                "capability_id": "gwas.associate", "step_id": "assoc",
                "inputs": {"cohort": cohort_ref},
                "parameters": {"model": "linear"},
            },
        )
        naive_stats_ref = artifact_ref(naive.result, "stats")
        # The diagnostic the agent acts on is read back out of the artifact,
        # not handed over as a return value. Artifacts are the medium.
        naive_lambda = read_json(session, naive_stats_ref)["lambda_gc"]

        naive_hits = await broker.dispatch(
            "run_capability",
            {
                "capability_id": "gwas.correct", "step_id": "correct",
                "inputs": {"stats": naive_stats_ref},
                "parameters": {"alpha": 0.05},
            },
        )
        naive_list = read_json(session, artifact_ref(naive_hits.result, "hits"))
        truly_causal = set(data.CAUSAL)
        fdr_hits = [row["marker"] for row in naive_list["hits"] if row["fdr"]]
        spurious = [marker for marker in fdr_hits if marker not in truly_causal]

        show("markers tested", naive_list["markers_tested"])
        show("genomic inflation λ", f"{naive_lambda}   ← a calibrated scan sits near 1.0")
        show("survive FDR", f"{len(fdr_hits)}")
        show("…of which are real", f"{len(fdr_hits) - len(spurious)} of {len(fdr_hits)}")
        show("false positives", ", ".join(spurious) or "none")

        # ── 7 ────────────────────────────────────────────────────────────────
        banner(7, "Agent-reported steps: the review that changes the plan")
        # `review` is one of the three kinds the agent may complete itself. It
        # succeeds — the check ran — while recording a finding that invalidates
        # the result the plan was built to produce.
        await broker.dispatch(
            "update_step",
            {
                "step_id": "review", "status": "succeeded",
                "summary": (
                    f"λ = {naive_lambda}. The whole test-statistic distribution is "
                    f"shifted, not a handful of loci. {len(spurious)} of {len(fdr_hits)} "
                    "FDR hits are ancestry, not biology. The model is misspecified."
                ),
            },
        )
        guarded = await broker.dispatch("update_step", {"step_id": "assoc", "status": "failed"})
        show("review recorded", parse_plan(session.current_plan()).progress)
        show("faking a capability step", f"refused — {guarded.error}")

        # The obvious fix is to rerun the same step with a better model. Two
        # separate things stop that, and both are worth seeing.
        in_place = await broker.dispatch(
            "run_capability",
            {
                "capability_id": "gwas.associate", "step_id": "assoc",
                "inputs": {"cohort": cohort_ref},
                "parameters": {"model": "mlm"},
            },
        )
        show("rerun 'assoc' in place", f"refused — {in_place.error}")

        # …and even if the step were re-runnable, the runner itself would refuse:
        # revision 1 has no step that produces a kinship matrix, so the better
        # model has nothing to condition on. Probing it directly through the
        # engine shows what the capability says for itself.
        probe = await engine.execute(
            graph_from_capability(
                registry.capability("gwas.associate"),
                sources={"cohort": (cohort_ref,)},
                parameters={"model": "mlm", "covariate_components": 2},
            )
        )
        show("what mlm says for itself", probe.failed_nodes[0]["error"])
        show("retryable?", "no — the same call fails identically, every time")
        show("conclusion", "the plan needs a step it does not have")

        # ── 8 ────────────────────────────────────────────────────────────────
        banner(8, "Replan discipline: revision 2 has to explain itself")
        corrected_plan = {
            "goal": naive_plan["goal"],
            "summary": "Model ancestry explicitly, then rescan with a kinship-corrected model.",
            "revision": 2,
            "reason": (
                f"λ = {naive_lambda} in revision 1: the association statistics are "
                "inflated genome-wide, which is population structure rather than "
                "signal. Adding ancestry axes and a relatedness matrix, and "
                "rescanning with a mixed model."
            ),
            "steps": [
                {"id": "qc", "title": "Quality control", "kind": "capability", "capability": "gwas.qc"},
                {"id": "pca", "title": "Ancestry axes", "kind": "capability", "capability": "gwas.pca", "depends_on": ["qc"]},
                {"id": "kinship", "title": "Relatedness matrix", "kind": "capability", "capability": "gwas.kinship", "depends_on": ["qc"]},
                {"id": "assoc", "title": "Structure-aware scan", "kind": "capability", "capability": "gwas.associate", "depends_on": ["qc", "pca", "kinship"]},
                {"id": "correct", "title": "Correct for multiple testing", "kind": "capability", "capability": "gwas.correct", "depends_on": ["assoc"]},
                {"id": "annotate", "title": "Annotate surviving markers", "kind": "capability", "capability": "gwas.annotate", "depends_on": ["correct"]},
                {"id": "summarise", "title": "Compose the study summary", "kind": "capability", "capability": "gwas.summarise", "depends_on": ["qc", "annotate"]},
                {"id": "review", "title": "Check the model is calibrated", "kind": "review", "depends_on": ["summarise"]},
                {"id": "answer", "title": "Report the associated loci", "kind": "answer", "depends_on": ["review"]},
            ],
        }
        show("revision 2 accepted", (await broker.dispatch("publish_plan", {"plan": corrected_plan})).ok)
        show("history retained", [item["revision"] for item in session.plan_history()])
        for index, layer in enumerate(parse_plan(session.current_plan()).layers):
            note = "   ← the engine may run these at once" if len(layer) > 1 else ""
            show(f"layer {index}", ", ".join(layer) + note)

        no_reason = await broker.dispatch(
            "publish_plan", {"plan": {**corrected_plan, "revision": 3, "reason": None}}
        )
        show("revision with no reason", f"refused — {no_reason.error}")
        stale = await broker.dispatch("publish_plan", {"plan": {**corrected_plan, "revision": 1}})
        show("non-increasing revision", f"refused — {stale.error}")

        # ── 9 ────────────────────────────────────────────────────────────────
        banner(9, "Artifact reuse across the replan")
        # Republishing a plan resets step *state*; it never touches the artifacts
        # already produced. The QC output from revision 1 is still a live source
        # ref, so revision 2 does not have to redo the work it inherited.
        inherited = read_json(session, qc_report_ref)
        show("QC artifact from revision 1", f"still resolvable — {inherited['markers_out']} markers")

        # It is re-run here anyway, from the *other* input variant, to show the
        # same contract accepting a VCF where revision 1 supplied a PLINK triple.
        requalified = await broker.dispatch(
            "run_capability",
            {
                "capability_id": "gwas.qc", "step_id": "qc",
                "inputs": {"vcf": vcf["source_ref"]},
            },
        )
        cohort_ref = artifact_ref(requalified.result, "cohort")
        qc_report_ref = artifact_ref(requalified.result, "qc_report")
        show("re-run from the VCF instead", f"same contract, other variant — {requalified.result['status']}")
        show("source recorded as", read_json(session, qc_report_ref)["source"])

        # ── 10 ───────────────────────────────────────────────────────────────
        banner(10, "Population structure: two branches off the same parent")
        pca = await broker.dispatch(
            "run_capability",
            {
                "capability_id": "gwas.pca", "step_id": "pca",
                "inputs": {"cohort": cohort_ref}, "parameters": {"components": 2},
            },
        )
        components_ref = artifact_ref(pca.result, "components")
        separation = read_json(session, components_ref)["pc1_ancestry_separation"]
        show("PC1 separates ancestry by", f"{separation} SD")

        grm = await broker.dispatch(
            "run_capability",
            {"capability_id": "gwas.kinship", "step_id": "kinship", "inputs": {"cohort": cohort_ref}},
        )
        grm_ref = artifact_ref(grm.result, "grm")
        grm_size = len(read_json(session, grm_ref)["samples"])
        show("relatedness matrix", f"{grm_size}×{grm_size}")
        show("note", "the broker runs agent calls one at a time — see section 13")

        # ── 11 ───────────────────────────────────────────────────────────────
        banner(11, "The corrected scan")
        mlm = await broker.dispatch(
            "run_capability",
            {
                "capability_id": "gwas.associate", "step_id": "assoc",
                "inputs": {"cohort": cohort_ref, "grm": grm_ref, "components": components_ref},
                "parameters": {"model": "mlm", "covariate_components": 2},
            },
        )
        mlm_lambda = read_json(session, artifact_ref(mlm.result, "stats"))["lambda_gc"]
        corrected = await broker.dispatch(
            "run_capability",
            {
                "capability_id": "gwas.correct", "step_id": "correct",
                "inputs": {"stats": artifact_ref(mlm.result, "stats")},
            },
        )
        hits_ref = artifact_ref(corrected.result, "hits")
        hit_list = read_json(session, hits_ref)
        recovered = [row["marker"] for row in hit_list["hits"]]

        show("genomic inflation λ", f"{naive_lambda}  →  {mlm_lambda}")
        show("survive correction", f"{len(fdr_hits)}  →  {len(recovered)}")
        show("recovered", ", ".join(recovered))
        show("markers with a real effect", ", ".join(sorted(truly_causal)))
        show("verdict", "every surviving marker is one that genuinely affects the phenotype"
             if set(recovered) == truly_causal else "partial recovery")

        # ── 12 ───────────────────────────────────────────────────────────────
        banner(12, "Retry with backoff, then fan-in")
        started = time.monotonic()
        annotated = await broker.dispatch(
            "run_capability",
            {"capability_id": "gwas.annotate", "step_id": "annotate", "inputs": {"hits": hits_ref}},
        )
        annotated_ref = artifact_ref(annotated.result, "annotated")
        show("catalogue status", annotated.result["status"])
        show("attempts used", f"{read_json(session, annotated_ref)['attempts_used']}"
                              " (two 503s, then success)")
        show("elapsed", f"{time.monotonic() - started:.2f}s  (0.2s + 0.4s of backoff)")

        summary = await broker.dispatch(
            "run_capability",
            {
                "capability_id": "gwas.summarise", "step_id": "summarise",
                "inputs": {
                    "qc_report": qc_report_ref,
                    "annotated": annotated_ref,
                    "components": components_ref,
                },
            },
        )
        summary_ref = artifact_ref(summary.result, "summary")
        show("summary composed from", "3 upstream artifacts")

        await broker.dispatch(
            "update_step",
            {"step_id": "review", "status": "succeeded",
             "summary": f"λ = {mlm_lambda}; the null is calibrated and the hits stand."},
        )
        await broker.dispatch(
            "update_step",
            {"step_id": "answer", "status": "succeeded",
             "summary": f"{len(recovered)} loci associated with salt tolerance."},
        )
        show("plan progress", parse_plan(session.current_plan()).progress)

        # ── 13 ───────────────────────────────────────────────────────────────
        banner(13, "Real engine parallelism (registered workflow)")
        # The broker refuses two *overlapping* agent-initiated executions, so
        # agent-level calls are sequential by design. Parallelism lives inside a
        # single execution graph: this SOP puts ancestry and relatedness on the
        # same dependency layer, so the engine runs them at the same time.
        workflow = registry.workflow("gwas.structured_scan")
        graph = graph_from_workflow(
            workflow,
            sources={"vcf": (vcf["source_ref"],)},
            parameters=workflow.validate_parameters({"model": "mlm", "covariate_components": 2}),
        )
        show("graph layers", " → ".join("+".join(layer) for layer in graph.layers))
        started = time.monotonic()
        run = await engine.execute(graph)
        show("status", run.status)
        show("nodes", {key: state.status for key, state in run.nodes.items()})
        show("λ from the SOP", run.nodes["assoc"].detail.get("lambda_gc"))
        show("wall clock", f"{time.monotonic() - started:.2f}s")

        # ── 14 ───────────────────────────────────────────────────────────────
        banner(14, "Human approval before a claim leaves the building")
        approval_graph = graph_from_capability(
            registry.capability("gwas.register_finding"),
            sources={"summary": (summary_ref,)},
            parameters={},
        )
        approval_run = engine.submit(approval_graph)
        for _ in range(400):
            await asyncio.sleep(0.005)
            if approval_run.pending_approvals:
                break
        show("run status", approval_run.status)
        show("waiting on", approval_run.pending_approvals)
        show("side effect performed?", "no — the runner returned before acting")

        approval_run.approve("execute", True)
        await approval_run.wait()
        show("after approval", approval_run.status)

        # ── 15 ───────────────────────────────────────────────────────────────
        banner(15, "Failure and skip propagation")
        failing = {
            "goal": "Demonstrate failure handling.",
            "revision": 3,
            "reason": "Show what happens when an upstream step fails.",
            "steps": [
                {"id": "load", "title": "QC an empty cohort", "kind": "capability", "capability": "gwas.qc"},
                {"id": "downstream", "title": "Scan it", "kind": "capability", "capability": "gwas.associate", "depends_on": ["load"]},
                {"id": "wrap_up", "title": "Answer", "kind": "answer", "depends_on": ["downstream"]},
            ],
        }
        await broker.dispatch("publish_plan", {"plan": failing})
        empty = session.save_upload("empty.vcf", b"sample\tancestry\tphenotype\trs1\n")
        failed = await broker.dispatch(
            "run_capability",
            {"capability_id": "gwas.qc", "step_id": "load", "inputs": {"vcf": empty["source_ref"]}},
        )
        statuses = {step["id"]: step["status"] for step in session.current_plan()["steps"]}
        show("load", f"{statuses['load']} — {failed.error}")
        show("downstream", f"{statuses['downstream']}  ← skipped, never ran")
        show("wrap_up", f"{statuses['wrap_up']}  ← skipped transitively")

        # ── 16 ───────────────────────────────────────────────────────────────
        banner(16, "Results and audit trail")
        for artifact in session.list_artifacts()[:10]:
            show(artifact["filename"], f"{artifact['size']:>7} B   {artifact['source_ref']}")
        show("artifacts total", len(session.list_artifacts()))
        show("events recorded", session.events.last_seq)
        show("hash chain intact", session.events.verify())

        summary_row = next(
            item for item in session.list_artifacts() if item["filename"].endswith(".md")
        )
        print(f"\n{DIM}{'─' * 68}{RESET}")
        print((session.root / summary_row["relpath"]).read_text())

        await broker.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
