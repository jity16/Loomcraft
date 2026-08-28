"""Structured missing-input request and checksum-aware fulfillment."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core"))

from loomcraft import InMemoryStore, Registry, ToolBroker  # noqa: E402


async def main() -> None:
    store = InMemoryStore()
    store.create_session("inputs-demo")
    broker = ToolBroker("inputs-demo", Registry(), store=store)
    response = await broker.dispatch_dynamic_tool("request_inputs", {"request": {
        "title": "Need source files",
        "message": "Upload a table and its index before continuing.",
        "requirements": [
            {"key": "table", "label": "CSV table", "description": "Rows to inspect", "required": True, "min_files": 1, "max_files": 1, "allowed_extensions": [".csv"]},
            {"key": "index", "label": "Index", "description": "Optional index", "required": False, "min_files": 0, "max_files": 1, "allowed_extensions": [".idx"]},
        ],
        "continue_prompt": "Files are ready; continue.",
    }})
    request_id = response["result"]["request"]["request_id"]
    allocation = broker.fulfill_inputs(request_id, [
        {"id": "table-1", "filename": "cohort.csv", "checksum": "sha-table"},
    ])
    print("Request:", request_id)
    print("Allocation:", allocation)
    print("Events:", [event.event for event in store.read_events("inputs-demo")])


if __name__ == "__main__":
    asyncio.run(main())

