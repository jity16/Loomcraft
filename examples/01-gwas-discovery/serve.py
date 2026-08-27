#!/usr/bin/env python3
"""Example 1 — serve the study over HTTP with a live SSE event stream.

    pip install 'loomcraft[server,anthropic]'
    export ANTHROPIC_API_KEY=...            # or: ant auth login
    python examples/01-gwas-discovery/serve.py

Then open http://127.0.0.1:8000/ for a browser workbench built on
``@loomcraft/renderer``'s state reducer, or drive the API directly:

    SID=$(curl -sX POST localhost:8000/api/v1/loomcraft/sessions | jq -r .session_id)
    curl -sF "file=@cohort.vcf" localhost:8000/api/v1/loomcraft/sessions/$SID/uploads
    curl -N -X POST localhost:8000/api/v1/loomcraft/sessions/$SID/turn \
      -H 'content-type: application/json' \
      -d '{"message":"Find markers associated with salt tolerance."}'

Pass ``--scripted`` to run without a model. The scripted agent walks the whole
discovery arc — naive scan, inflated lambda, replan to a structure-aware model —
so the UI has a revision switcher with something real in it, which is the part
worth looking at.

``--write-cohort`` drops a ``cohort.vcf`` next to this file so you have
something to upload.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[2] / "packages" / "core" / "src"))

import data  # noqa: E402
from capabilities import registry  # noqa: E402

from loomcraft import ScriptedAgent, SessionStore  # noqa: E402
from loomcraft.server import create_app  # noqa: E402

HERE = Path(__file__).parent

GOAL = "Find markers associated with salt tolerance in the uploaded cohort."

NAIVE_STEPS = [
    {"id": "qc", "title": "Quality control", "kind": "capability", "capability": "gwas.qc"},
    {"id": "assoc", "title": "Association scan", "kind": "capability", "capability": "gwas.associate", "depends_on": ["qc"]},
    {"id": "correct", "title": "Correct for multiple testing", "kind": "capability", "capability": "gwas.correct", "depends_on": ["assoc"]},
    {"id": "review", "title": "Check the model is calibrated", "kind": "review", "depends_on": ["correct"]},
    {"id": "answer", "title": "Report the associated loci", "kind": "answer", "depends_on": ["review"]},
]

CORRECTED_STEPS = [
    {"id": "qc", "title": "Quality control", "kind": "capability", "capability": "gwas.qc"},
    {"id": "pca", "title": "Ancestry axes", "kind": "capability", "capability": "gwas.pca", "depends_on": ["qc"]},
    {"id": "kinship", "title": "Relatedness matrix", "kind": "capability", "capability": "gwas.kinship", "depends_on": ["qc"]},
    {"id": "assoc", "title": "Structure-aware scan", "kind": "capability", "capability": "gwas.associate", "depends_on": ["qc", "pca", "kinship"]},
    {"id": "correct", "title": "Correct for multiple testing", "kind": "capability", "capability": "gwas.correct", "depends_on": ["assoc"]},
    {"id": "annotate", "title": "Annotate surviving markers", "kind": "capability", "capability": "gwas.annotate", "depends_on": ["correct"]},
    {"id": "summarise", "title": "Compose the study summary", "kind": "capability", "capability": "gwas.summarise", "depends_on": ["qc", "annotate"]},
    {"id": "review", "title": "Check the model is calibrated", "kind": "review", "depends_on": ["summarise"]},
    {"id": "answer", "title": "Report the associated loci", "kind": "answer", "depends_on": ["review"]},
]


def scripted_agent_for(session):
    """A no-model agent that reproduces the discovery arc against real artifacts.

    It is driven by what is actually in the session rather than by a step
    counter, so a refresh, a reconnect, or a slow runner cannot desynchronise it.
    """

    def by_filename() -> dict[str, str]:
        return {item["filename"]: item["source_ref"] for item in session.list_artifacts()}

    def read(source_ref: str) -> dict:
        return json.loads(session.resolve_source(source_ref).path.read_text())

    def script(responses):
        uploads = session.list_uploads()
        if not uploads:
            return []
        cohort = uploads[0]["source_ref"]

        if not responses:
            return [
                ("session_context", {}),
                ("inspect_source", {"source_ref": cohort, "max_lines": 1}),
            ]

        plan = session.current_plan() or {}
        revision = plan.get("revision", 0)
        files = by_filename()

        if revision == 0:
            return [("publish_plan", {"plan": {
                "goal": GOAL,
                "summary": "QC the genotypes, scan every marker, correct for multiple testing.",
                "revision": 1,
                "steps": NAIVE_STEPS,
            }})]

        if revision == 1:
            if "cohort.qc.tsv" not in files:
                return [("run_capability", {
                    "capability_id": "gwas.qc", "step_id": "qc",
                    "inputs": {"vcf": cohort},
                })]
            if "assoc.linear.json" not in files:
                return [("run_capability", {
                    "capability_id": "gwas.associate", "step_id": "assoc",
                    "inputs": {"cohort": files["cohort.qc.tsv"]},
                    "parameters": {"model": "linear"},
                })]
            if "hits.json" not in files:
                return [("run_capability", {
                    "capability_id": "gwas.correct", "step_id": "correct",
                    "inputs": {"stats": files["assoc.linear.json"]},
                })]

            # The diagnostic is read out of the artifact, and it is what makes
            # the agent abandon its own plan.
            inflation = read(files["assoc.linear.json"])["lambda_gc"]
            hits = read(files["hits.json"])["hits"]
            return [
                ("update_step", {
                    "step_id": "review", "status": "succeeded",
                    "summary": (
                        f"λ = {inflation}. The whole test-statistic distribution is "
                        f"shifted, so most of these {len(hits)} hits are ancestry "
                        "rather than biology. The model is misspecified."
                    ),
                }),
                ("publish_plan", {"plan": {
                    "goal": GOAL,
                    "summary": "Model ancestry explicitly, then rescan with a kinship-corrected model.",
                    "revision": 2,
                    "reason": (
                        f"λ = {inflation} in revision 1: the association statistics "
                        "are inflated genome-wide, which is population structure "
                        "rather than signal. Adding ancestry axes and a relatedness "
                        "matrix, and rescanning with a mixed model."
                    ),
                    "steps": CORRECTED_STEPS,
                }}),
            ]

        # Revision 2 — the structure-aware pass.
        if "cohort.qc.tsv" not in files:
            return [("run_capability", {
                "capability_id": "gwas.qc", "step_id": "qc", "inputs": {"vcf": cohort},
            })]
        if "pca.json" not in files:
            return [("run_capability", {
                "capability_id": "gwas.pca", "step_id": "pca",
                "inputs": {"cohort": files["cohort.qc.tsv"]}, "parameters": {"components": 2},
            })]
        if "kinship.json" not in files:
            return [("run_capability", {
                "capability_id": "gwas.kinship", "step_id": "kinship",
                "inputs": {"cohort": files["cohort.qc.tsv"]},
            })]
        if "assoc.mlm.json" not in files:
            return [("run_capability", {
                "capability_id": "gwas.associate", "step_id": "assoc",
                "inputs": {
                    "cohort": files["cohort.qc.tsv"],
                    "grm": files["kinship.json"],
                    "components": files["pca.json"],
                },
                "parameters": {"model": "mlm", "covariate_components": 2},
            })]
        if "annotated-hits.json" not in files:
            # `hits.json` exists from revision 1, so correct has to run again and
            # overwrite it before annotation can be trusted.
            corrected = read(files["hits.json"]).get("model") == "mlm"
            if not corrected:
                return [("run_capability", {
                    "capability_id": "gwas.correct", "step_id": "correct",
                    "inputs": {"stats": files["assoc.mlm.json"]},
                })]
            return [("run_capability", {
                "capability_id": "gwas.annotate", "step_id": "annotate",
                "inputs": {"hits": files["hits.json"]},
            })]
        if "study-summary.md" not in files:
            return [("run_capability", {
                "capability_id": "gwas.summarise", "step_id": "summarise",
                "inputs": {
                    "qc_report": files["qc-report.json"],
                    "annotated": files["annotated-hits.json"],
                    "components": files["pca.json"],
                },
            })]

        inflation = read(files["assoc.mlm.json"])["lambda_gc"]
        markers = [row["marker"] for row in read(files["annotated-hits.json"])["hits"]]
        if plan.get("steps") and any(
            step["id"] == "answer" and step["status"] != "succeeded" for step in plan["steps"]
        ):
            return [
                ("update_step", {
                    "step_id": "review", "status": "succeeded",
                    "summary": f"λ = {inflation}; the null is calibrated and the hits stand.",
                }),
                ("update_step", {
                    "step_id": "answer", "status": "succeeded",
                    "summary": f"{len(markers)} loci: {', '.join(markers)}.",
                }),
            ]
        return []

    return ScriptedAgent(
        script,
        final_text=(
            "The first scan was confounded by population structure; the "
            "structure-aware rescan is in the summary artifact."
        ),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data", default="./.loomcraft-data")
    parser.add_argument("--scripted", action="store_true", help="Run without a model.")
    parser.add_argument("--model", default="claude-opus-5")
    parser.add_argument(
        "--write-cohort", action="store_true",
        help="Write a cohort.vcf next to this file and exit.",
    )
    args = parser.parse_args()

    if args.write_cohort:
        target = HERE / "cohort.vcf"
        target.write_text(data.as_matrix(data.build_cohort()))
        print(f"wrote {target}")
        return 0

    try:
        import uvicorn
    except ImportError:
        print("This example needs the server extra: pip install 'loomcraft[server]'")
        return 2

    store = SessionStore(Path(args.data))

    if args.scripted:
        agent_factory = scripted_agent_for
    else:
        from loomcraft import AnthropicAgent

        agent = AnthropicAgent(model=args.model)
        agent_factory = lambda _session: agent  # noqa: E731

    app = create_app(store, registry, agent_factory, title="LoomCraft · association study")

    # Serve the single-file demo UI at the root.
    from fastapi.responses import FileResponse

    @app.get("/")
    async def index():
        return FileResponse(HERE / "web" / "index.html")

    print(f"LoomCraft on http://{args.host}:{args.port}  "
          f"({'scripted' if args.scripted else args.model})")
    print("upload a cohort with:  python serve.py --write-cohort")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
