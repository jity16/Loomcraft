#!/usr/bin/env python3
"""Example 2 — the same scenario, driven by a real Claude agent.

    pip install 'loomcraft[anthropic]'
    export ANTHROPIC_API_KEY=...        # or: ant auth login
    python examples/02-research-assistant/run_live.py

Compare this file with ``run_scripted.py``: the capabilities, the broker, the
engine, and the events are identical. The only difference is who decides which
tool to call next. That is the point of the split — you develop and test against
``ScriptedAgent``, then change one line for production.

Add ``--one-document`` to withhold the second file and watch a real model
discover the gap, call ``request_inputs``, and (after you decline) replan around
the failure on its own.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parents[2] / "packages" / "core" / "src"))

from capabilities import registry  # noqa: E402
from run_scripted import DOC_A, DOC_B  # noqa: E402

from loomcraft import AnthropicAgent, SessionStore, ToolBroker, parse_plan  # noqa: E402

BOLD, DIM, RESET = "\033[1m", "\033[2m", "\033[0m"


def make_event_printer():
    """Render the same event stream the browser would receive."""
    streaming = {"active": False}

    def on_event(name: str, data) -> None:
        if name == "message_delta":
            if not streaming["active"]:
                print(f"\n{BOLD}agent:{RESET} ", end="", flush=True)
                streaming["active"] = True
            print(data.get("delta", ""), end="", flush=True)
            return
        if streaming["active"]:
            print()
            streaming["active"] = False

        if name == "tool_call":
            print(f"{DIM}  → {data.get('tool')}{RESET}", flush=True)
        elif name == "tool_result":
            mark = "ok" if data.get("ok") else f"REFUSED ({data.get('error_code')})"
            detail = "" if data.get("ok") else f" — {data.get('error')}"
            print(f"{DIM}  ← {data.get('tool')}: {mark}{detail}{RESET}", flush=True)
        elif name == "plan_published":
            plan = parse_plan(data["plan"])
            print(f"\n{BOLD}plan R{plan.revision}{RESET}: {plan.goal}")
            if plan.reason:
                print(f"  {DIM}reason: {plan.reason}{RESET}")
            for index, layer in enumerate(plan.layers):
                note = "   ← concurrent" if len(layer) > 1 else ""
                print(f"  layer {index}: {', '.join(layer)}{note}")
        elif name == "step_updated":
            step = data["step"]
            print(f"{DIM}  ▸ {step['id']}: {step['status']}{RESET}")
        elif name == "execution_finished":
            execution = data["execution"]
            print(
                f"{DIM}  ⚙ {execution['capability']}: {execution['status']} "
                f"({len(execution['artifacts'])} artifact(s)){RESET}"
            )
        elif name == "input_required":
            request = data["request"]
            print(f"\n{BOLD}files requested:{RESET} {request['title']}")
            for requirement in request["requirements"]:
                print(f"  · {requirement['label']} ({', '.join(requirement['allowed_extensions'])})")
        elif name == "error":
            print(f"\n{BOLD}error:{RESET} {data.get('message')}")

    return on_event


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--one-document",
        action="store_true",
        help="Withhold the second document so the agent must ask for it.",
    )
    parser.add_argument("--model", default="claude-opus-5")
    parser.add_argument(
        "--effort",
        default="high",
        choices=["low", "medium", "high", "xhigh", "max"],
    )
    parser.add_argument(
        "--decline",
        action="store_true",
        help="With --one-document, decline the request instead of supplying the file.",
    )
    args = parser.parse_args()

    try:
        import anthropic  # noqa: F401,PLC0415
    except ImportError:
        print("This example needs the Anthropic SDK: pip install 'loomcraft[anthropic]'")
        return 2

    with TemporaryDirectory() as tmp:
        store = SessionStore(Path(tmp) / "sessions")
        session = store.create("research-live")
        session.save_upload("q3-migration-engineering.md", DOC_A.encode())
        if not args.one_document:
            session.save_upload("q3-migration-product.md", DOC_B.encode())

        broker = ToolBroker(session, registry)
        agent = AnthropicAgent(model=args.model, effort=args.effort)
        on_event = make_event_printer()

        print(f"{BOLD}model{RESET}   {args.model} (effort={args.effort})")
        print(f"{BOLD}files{RESET}   {[u['filename'] for u in session.list_uploads()]}")
        print(f"{DIM}{'─' * 66}{RESET}")

        result = await agent.run_turn(
            broker,
            "Produce a comparative research brief across the uploaded Q3 migration "
            "reports. Call out anywhere the reports disagree.",
            on_event=on_event,
        )

        # If the agent asked for files, respond the way a user would and let it
        # continue in a second turn — with the same broker and the same session.
        pending = broker.awaiting_inputs
        if pending:
            from loomcraft.inputs import pending_requests

            requests = pending_requests(
                [event.to_dict() for event in session.events.read()]
            )
            request_id = requests[0]["request_id"]
            print(f"\n{DIM}{'─' * 66}{RESET}")
            if args.decline:
                broker.cancel_input_request(request_id)
                follow_up = (
                    "I don't have a second document. Please continue with what you "
                    "have and be explicit about the limitation."
                )
                print(f"{BOLD}user declines the request{RESET}")
            else:
                session.save_upload("q3-migration-product.md", DOC_B.encode())
                broker.fulfill_input_request(request_id)
                follow_up = requests[0]["continue_prompt"]
                print(f"{BOLD}user uploads the second document{RESET}")
            print(f"{DIM}{'─' * 66}{RESET}")

            result = await agent.run_turn(
                broker,
                follow_up,
                history=result.messages,
                on_event=on_event,
            )

        print(f"\n{DIM}{'─' * 66}{RESET}")
        plan = session.current_plan()
        if plan:
            parsed = parse_plan(plan)
            print(f"{BOLD}final plan{RESET}  R{parsed.revision}  {parsed.progress}")
        print(f"{BOLD}artifacts{RESET}  {[a['filename'] for a in session.list_artifacts()]}")
        print(f"{BOLD}tokens{RESET}     {result.usage}")
        print(f"{BOLD}audit{RESET}      {session.events.last_seq} events, "
              f"chain intact: {session.events.verify()}")

        for artifact in session.list_artifacts():
            if artifact["filename"].endswith(".md") and "brief" in artifact["filename"]:
                print(f"\n{DIM}{'─' * 66}{RESET}")
                print((session.root / artifact["relpath"]).read_text())

        await broker.close()
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
