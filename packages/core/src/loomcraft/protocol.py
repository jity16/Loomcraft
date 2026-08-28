"""Framework-neutral JSON-RPC bridge for AI app-server integrations."""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping

from .broker import ToolBroker
from .tools import dynamic_tool_specs


class ProtocolError(ValueError):
    pass


class AppServerBridge:
    """Translate common JSON-RPC tool calls to one ToolBroker.

    This is intentionally transport-agnostic: a host can feed messages from a
    Codex app-server subprocess, WebSocket, or HTTP endpoint and write the
    returned dictionaries back on the same channel.
    """

    def __init__(self, broker: ToolBroker) -> None:
        self.broker = broker

    async def handle(self, message: Mapping[str, Any]) -> Dict[str, Any]:
        if not isinstance(message, Mapping):
            raise ProtocolError("JSON-RPC message must be an object")
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") if isinstance(message.get("params"), Mapping) else {}
        if request_id is None and isinstance(params.get("call_id"), (str, int)):
            request_id = params.get("call_id")
        if method in {"initialize", "session/initialize"}:
            return {"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": "loomcraft-v1", "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "loomcraft", "version": "0.1.0"}}}
        if method in {"tools/list", "item/tool/list"}:
            return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": dynamic_tool_specs()}}
        if method in {"tools/call", "item/tool/call"}:
            tool = params.get("name") or params.get("tool")
            arguments = params.get("arguments", params.get("input", {}))
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except ValueError:
                    arguments = None
            if not isinstance(tool, str) or not isinstance(arguments, Mapping):
                return self._error(request_id, -32602, "tool name and object arguments are required")
            result = await self.broker.dispatch_dynamic_tool(tool, arguments)
            if request_id is None:
                return {}
            return {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False)}], "structuredContent": result, "isError": not result.get("ok", False)}}
        if request_id is None:
            return {}
        return self._error(request_id, -32601, "method not found")

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}
