"""A JSON-RPC bridge so an app-server runtime can drive one broker.

Codex — and any host that speaks the same app-server shape — runs the model in
its own process and calls *back* for tools. The host owns the transport (a
long-lived subprocess over stdio, a WebSocket, an HTTP endpoint); this module
owns the translation, so a message arriving on that transport becomes a
validated :class:`~loomcraft.broker.ToolBroker` call and a reply.

The wiring on the host side is three steps::

    bridge = AppServerBridge(broker)

    # 1. advertise the tools when the turn starts
    tools = dynamic_tool_specs()

    # 2. every inbound JSON-RPC message goes through the bridge
    reply = await bridge.handle(message)

    # 3. write `reply` back on the same channel (skip empty dicts —
    #    those were notifications, which take no response)

Nothing here loosens the broker's guarantees. A tool call arriving over
JSON-RPC is validated against the published plan exactly like one arriving from
an in-process agent loop; the transport does not become a second door.
"""

from __future__ import annotations

import json
from typing import Any, Mapping

from .broker import ToolBroker
from .tools import dynamic_tool_specs

PROTOCOL_VERSION = "loomcraft-v1"

#: Method names treated as equivalent. The bare ``tools/*`` spellings are the
#: MCP-style names; the ``item/tool/*`` ones are what a Codex app-server sends.
_INITIALIZE = frozenset({"initialize", "session/initialize"})
_LIST_TOOLS = frozenset({"tools/list", "item/tool/list"})
_CALL_TOOL = frozenset({"tools/call", "item/tool/call"})


class ProtocolError(ValueError):
    """A message was not a well-formed JSON-RPC request."""


class AppServerBridge:
    """Translate JSON-RPC tool traffic into one session's broker calls."""

    def __init__(self, broker: ToolBroker, *, version: str = "0.1.0") -> None:
        self.broker = broker
        self.version = version

    async def handle(self, message: Mapping[str, Any]) -> dict[str, Any]:
        """Handle one inbound message.

        Returns the JSON-RPC response to write back, or an empty dict for a
        notification (a message with no ``id``), which takes no reply.
        """
        if not isinstance(message, Mapping):
            raise ProtocolError("JSON-RPC message must be an object")
        method = message.get("method")
        request_id = message.get("id")
        raw_params = message.get("params")
        params: Mapping[str, Any] = (
            raw_params if isinstance(raw_params, Mapping) else {}
        )
        # Some hosts carry the correlation id inside params instead of at the
        # envelope level.
        if request_id is None and isinstance(params.get("call_id"), (str, int)):
            request_id = params.get("call_id")

        if method in _INITIALIZE:
            return self._result(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "loomcraft", "version": self.version},
                },
            )

        if method in _LIST_TOOLS:
            return self._result(request_id, {"tools": dynamic_tool_specs()})

        if method in _CALL_TOOL:
            return await self._call_tool(request_id, params)

        if request_id is None:
            return {}
        return self._error(request_id, -32601, f"unknown method {method!r}")

    async def _call_tool(
        self, request_id: Any, params: Mapping[str, Any]
    ) -> dict[str, Any]:
        name = params.get("name") or params.get("tool")
        arguments: Any = params.get("arguments", params.get("input", {}))
        if isinstance(arguments, str):
            # Several runtimes send tool arguments as a JSON string.
            try:
                arguments = json.loads(arguments)
            except ValueError:
                arguments = None
        if not isinstance(name, str) or not isinstance(arguments, Mapping):
            return self._error(
                request_id, -32602, "tool name and object arguments are required"
            )

        result = await self.broker.dispatch_dynamic_tool(name, arguments)
        if request_id is None:
            return {}
        return self._result(
            request_id,
            {
                # Both shapes, because hosts differ on which they read: text for
                # models that want a content block, structured for those that
                # parse the object.
                "content": [
                    {"type": "text", "text": json.dumps(result, ensure_ascii=False)}
                ],
                "structuredContent": result,
                "isError": not result.get("ok", False),
            },
        )

    @staticmethod
    def _result(request_id: Any, payload: dict[str, Any]) -> dict[str, Any]:
        if request_id is None:
            return {}
        return {"jsonrpc": "2.0", "id": request_id, "result": payload}

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }


__all__ = ["AppServerBridge", "PROTOCOL_VERSION", "ProtocolError"]
