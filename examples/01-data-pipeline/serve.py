#!/usr/bin/env python3
"""Example 1 — serve the pipeline over HTTP with a live SSE event stream.

    pip install 'loomcraft[server,anthropic]'
    export ANTHROPIC_API_KEY=...            # or: ant auth login
    python examples/01-data-pipeline/serve.py

Then open http://127.0.0.1:8000/ for a browser workbench built on
``@loomcraft/renderer``'s state reducer, or drive the API directly:

    SID=$(curl -sX POST localhost:8000/api/v1/loomcraft/sessions | jq -r .session_id)
    curl -sF "file=@sales.csv" localhost:8000/api/v1/loomcraft/sessions/$SID/uploads
    curl -N -X POST localhost:8000/api/v1/loomcraft/sessions/$SID/turn \
      -H 'content-type: application/json' \
      -d '{"message":"Assess the quality of the uploaded table."}'

Pass ``--scripted`` to run without a model: a deterministic agent replays the
same tool calls, which is enough to exercise the whole UI.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[2] / "packages" / "core" / "src"))

from capabilities import registry  # noqa: E402

from loomcraft import ScriptedAgent, SessionStore  # noqa: E402
from loomcraft.server import create_app  # noqa: E402

HERE = Path(__file__).parent


def scripted_agent_for(session):
    """A no-model agent that plans and executes against whatever was uploaded."""

    def script(responses):
        uploads = session.list_uploads()
        if not uploads:
            return []
        table = uploads[0]["source_ref"]

        if not responses:
            return [
                ("session_context", {}),
                ("inspect_source", {"source_ref": table, "max_lines": 3}),
            ]
        if len(responses) == 2:
            return [
                (
                    "publish_plan",
                    {
                        "plan": {
                            "goal": "Assess the quality of the uploaded table.",
                            "summary": "Clean, then profile and scan in parallel, then report.",
                            "revision": 1,
                            "steps": [
                                {"id": "clean", "title": "Clean the table", "kind": "capability", "capability": "csv.clean"},
                                {"id": "profile", "title": "Profile columns", "kind": "capability", "capability": "csv.profile", "depends_on": ["clean"]},
                                {"id": "outliers", "title": "Detect outliers", "kind": "capability", "capability": "csv.outliers", "depends_on": ["clean"]},
                                {"id": "report", "title": "Compose the report", "kind": "capability", "capability": "csv.report", "depends_on": ["profile", "outliers"]},
                                {"id": "answer", "title": "Answer", "kind": "answer", "depends_on": ["report"]},
                            ],
                        }
                    },
                )
            ]
        if len(responses) == 3:
            return [("run_capability", {"capability_id": "csv.clean", "step_id": "clean", "inputs": {"table": table}})]

        artifacts = {item["port_name"]: item["source_ref"] for item in session.list_artifacts()}
        if "cleaned" in artifacts and "profile" not in artifacts:
            return [("run_capability", {"capability_id": "csv.profile", "step_id": "profile", "inputs": {"cleaned": artifacts["cleaned"]}})]
        if "profile" in artifacts and "outliers" not in artifacts:
            return [("run_capability", {"capability_id": "csv.outliers", "step_id": "outliers", "inputs": {"cleaned": artifacts["cleaned"]}, "parameters": {"z_threshold": 2.0}})]
        if "outliers" in artifacts and "report" not in artifacts:
            return [("run_capability", {"capability_id": "csv.report", "step_id": "report", "inputs": {"profile": artifacts["profile"], "outliers": artifacts["outliers"]}})]
        if "report" in artifacts:
            return [("update_step", {"step_id": "answer", "status": "succeeded", "summary": "Quality report delivered."})]
        return []

    return ScriptedAgent(script, final_text="The quality report is ready to download.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--data", default="./.loomcraft-data")
    parser.add_argument("--scripted", action="store_true", help="Run without a model.")
    parser.add_argument("--model", default="claude-opus-5")
    args = parser.parse_args()

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

    app = create_app(store, registry, agent_factory, title="LoomCraft · CSV quality")

    # Serve the single-file demo UI at the root.
    from fastapi.responses import FileResponse

    @app.get("/")
    async def index():
        return FileResponse(HERE / "web" / "index.html")

    print(f"LoomCraft on http://{args.host}:{args.port}  "
          f"({'scripted' if args.scripted else args.model})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
