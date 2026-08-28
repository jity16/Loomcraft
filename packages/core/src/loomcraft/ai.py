"""AI provider adapters and the model-to-tool planning loop.

The engine speaks a deliberately tiny normalized protocol.  OpenAI-compatible
Chat Completions and Responses endpoints, local scripted models, and JSONL
subprocess adapters can all plug into it without leaking provider details into
the DAG executor.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, Tuple, Union

from .broker import ToolBroker
from .tools import dynamic_tool_specs as canonical_dynamic_tool_specs


def dynamic_tool_specs() -> List[Dict[str, Any]]:
    """Compatibility view of the canonical provider-neutral tool catalog.

    The canonical implementation keeps schemas as typed tool-spec objects;
    the extracted provider loop historically consumed OpenAI-shaped mappings.
    Keep that conversion at this boundary so there is still one source of truth
    for tool definitions.
    """
    return canonical_dynamic_tool_specs()


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: Dict[str, Any]


@dataclass
class AIResponse:
    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    finish_reason: Optional[str] = None
    usage: Dict[str, Any] = field(default_factory=dict)
    raw: Optional[Dict[str, Any]] = None


@dataclass
class AIStreamEvent:
    """Normalized streaming delta (providers may emit only a subset)."""

    type: str
    text: str = ""
    tool_call: Optional[ToolCall] = None
    response: Optional[AIResponse] = None


class AIProvider(Protocol):
    async def complete(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        *,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
    ) -> AIResponse: ...

class StreamingAIProvider(AIProvider, Protocol):
    async def stream(
        self,
        messages: Sequence[Mapping[str, Any]],
        tools: Sequence[Mapping[str, Any]],
        *,
        model: Optional[str] = None,
        temperature: Optional[float] = None,
    ) -> AsyncIterator[AIStreamEvent]: ...


class AIProviderError(RuntimeError):
    """A provider request or response could not be completed."""


def _json_arguments(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise AIProviderError("model returned invalid tool arguments") from exc
        if isinstance(parsed, Mapping):
            return dict(parsed)
    raise AIProviderError("model tool arguments must be an object")


def _content_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        chunks: List[str] = []
        for item in value:
            if isinstance(item, str):
                chunks.append(item)
            elif isinstance(item, Mapping) and isinstance(item.get("text"), str):
                chunks.append(str(item["text"]))
        return "".join(chunks)
    return ""


def parse_chat_response(payload: Mapping[str, Any]) -> AIResponse:
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        raise AIProviderError("Chat Completions response has no choices")
    choice = choices[0] if isinstance(choices[0], Mapping) else {}
    message = choice.get("message") if isinstance(choice.get("message"), Mapping) else {}
    calls: List[ToolCall] = []
    raw_calls = message.get("tool_calls")
    if isinstance(raw_calls, list):
        for index, raw in enumerate(raw_calls):
            if not isinstance(raw, Mapping):
                continue
            function = raw.get("function") if isinstance(raw.get("function"), Mapping) else raw
            name = function.get("name") if isinstance(function, Mapping) else None
            if not isinstance(name, str) or not name:
                continue
            calls.append(ToolCall(
                id=str(raw.get("id") or "call-%d" % index),
                name=name,
                arguments=_json_arguments(function.get("arguments", {})),
            ))
    # A few compatible gateways still return the legacy function_call field.
    legacy = message.get("function_call")
    if not calls and isinstance(legacy, Mapping) and isinstance(legacy.get("name"), str):
        calls.append(ToolCall("call-0", str(legacy["name"]), _json_arguments(legacy.get("arguments", {}))))
    return AIResponse(
        text=_content_text(message.get("content")),
        tool_calls=calls,
        finish_reason=str(choice.get("finish_reason")) if choice.get("finish_reason") is not None else None,
        usage=dict(payload.get("usage", {})) if isinstance(payload.get("usage"), Mapping) else {},
        raw=dict(payload),
    )


def parse_responses_response(payload: Mapping[str, Any]) -> AIResponse:
    calls: List[ToolCall] = []
    text_chunks: List[str] = []
    output = payload.get("output")
    if isinstance(output, list):
        for index, item in enumerate(output):
            if not isinstance(item, Mapping):
                continue
            item_type = item.get("type")
            if item_type in {"function_call", "custom_tool_call"}:
                name = item.get("name") or item.get("tool_name")
                if isinstance(name, str) and name:
                    calls.append(ToolCall(
                        id=str(item.get("call_id") or item.get("id") or "call-%d" % index),
                        name=name,
                        arguments=_json_arguments(item.get("arguments", item.get("input", {}))),
                    ))
            elif item_type in {"message", "output_text"}:
                content = item.get("content", item.get("text", ""))
                text_chunks.append(_content_text(content))
    if not text_chunks and isinstance(payload.get("output_text"), str):
        text_chunks.append(str(payload["output_text"]))
    return AIResponse(
        text="".join(text_chunks),
        tool_calls=calls,
        finish_reason=str(payload.get("status")) if payload.get("status") is not None else None,
        usage=dict(payload.get("usage", {})) if isinstance(payload.get("usage"), Mapping) else {},
        raw=dict(payload),
    )


def openai_tool_specs(specs: Optional[Sequence[Mapping[str, Any]]] = None) -> List[Dict[str, Any]]:
    """Convert app-server specs to the OpenAI ``tools`` shape."""
    result: List[Dict[str, Any]] = []
    for item in specs or dynamic_tool_specs():
        if isinstance(item.get("function"), Mapping):
            result.append({"type": "function", "function": dict(item["function"])})
        else:
            result.append({
                "type": "function",
                "function": {
                    "name": item.get("name"),
                    "description": item.get("description", ""),
                    "parameters": item.get("inputSchema", {"type": "object"}),
                },
            })
    return result


class OpenAICompatibleProvider:
    """Dependency-free provider for Chat Completions or Responses APIs.

    It uses ``urllib`` so the core package remains installable in small worker
    images.  Applications may subclass it or inject an ``httpx`` implementation
    when they need connection pooling or streaming.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.openai.com/v1",
        model: str = "gpt-4.1-mini",
        protocol: str = "chat",
        timeout: float = 120.0,
        allow_insecure_http: bool = False,
        extra_params: Optional[Mapping[str, Any]] = None,
    ) -> None:
        if not api_key or len(api_key) > 4096 or any(ord(char) < 32 for char in api_key):
            raise ValueError("api_key is required")
        parsed = urllib.parse.urlsplit(base_url)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url must be a credential-free HTTP(S) URL")
        host = (parsed.hostname or "").lower()
        if parsed.scheme != "https" and not allow_insecure_http and host not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("remote HTTP requires allow_insecure_http=True")
        if protocol not in {"chat", "responses"}:
            raise ValueError("protocol must be chat or responses")
        if not isinstance(model, str) or not model.strip() or len(model) > 300 or any(ord(char) < 32 for char in model):
            raise ValueError("model is invalid")
        self.api_key = api_key
        normalized_base = base_url.rstrip("/")
        for suffix in ("/chat/completions", "/responses"):
            if normalized_base.endswith(suffix):
                normalized_base = normalized_base[: -len(suffix)]
                break
        self.base_url = normalized_base
        self.model = model
        self.protocol = protocol
        if not isinstance(timeout, (int, float)) or not math.isfinite(float(timeout)) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        self.timeout = max(0.1, float(timeout))
        self.extra_params = dict(extra_params or {})

    def __repr__(self) -> str:
        return "OpenAICompatibleProvider(protocol=%r, model=%r, base_url=%r)" % (self.protocol, self.model, self.base_url)

    def _request_payload(self, messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]], model: Optional[str], temperature: Optional[float]) -> Dict[str, Any]:
        chosen = model or self.model
        if self.protocol == "chat":
            payload: Dict[str, Any] = {"model": chosen, "messages": [dict(item) for item in messages], "tools": openai_tool_specs(tools), "tool_choice": "auto"}
            if temperature is not None:
                payload["temperature"] = temperature
            payload.update({key: value for key, value in self.extra_params.items() if key not in {"model", "messages", "tools", "tool_choice"}})
            return payload
        # Responses uses ``input`` and function tools without the outer
        # function wrapper.
        converted: List[Dict[str, Any]] = []
        for tool in openai_tool_specs(tools):
            function = tool.get("function", {})
            converted.append({"type": "function", **dict(function)})
        instructions: List[str] = []
        input_items: List[Dict[str, Any]] = []
        for message in messages:
            role = message.get("role")
            if role == "system":
                content = _content_text(message.get("content"))
                if content:
                    instructions.append(content)
                continue
            if role == "assistant" and isinstance(message.get("tool_calls"), list):
                content = _content_text(message.get("content"))
                if content:
                    input_items.append({"role": "assistant", "content": content})
                for call in message.get("tool_calls", []):
                    if not isinstance(call, Mapping):
                        continue
                    function = call.get("function") if isinstance(call.get("function"), Mapping) else {}
                    input_items.append({"type": "function_call", "call_id": call.get("id"), "name": function.get("name"), "arguments": function.get("arguments", "{}")})
                continue
            if role == "tool":
                input_items.append({"type": "function_call_output", "call_id": message.get("tool_call_id"), "output": message.get("content", "")})
                continue
            input_items.append(dict(message))
        payload = {"model": chosen, "input": input_items, "tools": converted, "tool_choice": "auto"}
        if instructions:
            payload["instructions"] = "\n\n".join(instructions)
        if temperature is not None:
            payload["temperature"] = temperature
        payload.update({key: value for key, value in self.extra_params.items() if key not in {"model", "input", "tools", "tool_choice"}})
        return payload

    def _request_sync(self, payload: Mapping[str, Any]) -> Dict[str, Any]:
        endpoint = self.base_url + ("/chat/completions" if self.protocol == "chat" else "/responses")
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        request = urllib.request.Request(endpoint, data=body, method="POST", headers={"Content-Type": "application/json", "Authorization": "Bearer %s" % self.api_key, "Accept": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(16 * 1024 * 1024)
        except urllib.error.HTTPError as exc:
            raise AIProviderError("AI provider returned HTTP %d" % exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AIProviderError("AI provider request failed: %s" % str(exc)[:1000]) from exc
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise AIProviderError("AI provider returned invalid JSON") from exc
        if not isinstance(value, Mapping):
            raise AIProviderError("AI provider response must be an object")
        return dict(value)

    async def complete(self, messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]], *, model: Optional[str] = None, temperature: Optional[float] = None) -> AIResponse:
        payload = self._request_payload(messages, tools, model, temperature)
        raw = await asyncio.to_thread(self._request_sync, payload)
        return parse_chat_response(raw) if self.protocol == "chat" else parse_responses_response(raw)

    def _stream_sync(self, payload: Mapping[str, Any], emit: Optional[Callable[[Tuple[str, Dict[str, Any]]], None]] = None) -> List[Tuple[str, Dict[str, Any]]]:
        endpoint = self.base_url + ("/chat/completions" if self.protocol == "chat" else "/responses")
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        request = urllib.request.Request(endpoint, data=body, method="POST", headers={"Content-Type": "application/json", "Authorization": "Bearer %s" % self.api_key, "Accept": "text/event-stream"})
        rows: List[Tuple[str, Dict[str, Any]]] = []
        total_bytes = 0
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                for raw_line in response:
                    total_bytes += len(raw_line)
                    if total_bytes > 16 * 1024 * 1024:
                        raise AIProviderError("AI provider stream exceeded the size limit")
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    text = line[5:].strip()
                    if text == "[DONE]":
                        item = ("done", {})
                        if emit is None:
                            rows.append(item)
                        if emit is not None:
                            emit(item)
                        continue
                    try:
                        value = json.loads(text)
                    except ValueError:
                        continue
                    if isinstance(value, Mapping):
                        item = ("data", dict(value))
                        if emit is None:
                            rows.append(item)
                        if emit is not None:
                            emit(item)
        except urllib.error.HTTPError as exc:
            raise AIProviderError("AI provider stream returned HTTP %d" % exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise AIProviderError("AI provider stream failed: %s" % str(exc)[:1000]) from exc
        return rows

    async def stream(self, messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]], *, model: Optional[str] = None, temperature: Optional[float] = None) -> AsyncIterator[AIStreamEvent]:
        payload = self._request_payload(messages, tools, model, temperature)
        payload["stream"] = True
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        sentinel = object()

        def emit(item: Tuple[str, Dict[str, Any]]) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, item)

        def worker() -> None:
            try:
                self._stream_sync(payload, emit)
            except BaseException as exc:  # forwarded onto the async iterator
                loop.call_soon_threadsafe(queue.put_nowait, exc)
            finally:
                loop.call_soon_threadsafe(queue.put_nowait, sentinel)

        producer = asyncio.create_task(asyncio.to_thread(worker))
        text_chunks: List[str] = []
        calls: Dict[str, Dict[str, Any]] = {}
        call_indices: Dict[str, str] = {}
        saw_done = False
        while True:
            queued = await queue.get()
            if queued is sentinel:
                break
            if isinstance(queued, BaseException):
                await producer
                raise queued
            kind, value = queued
            if kind == "done":
                saw_done = True
                final = AIResponse(text="".join(text_chunks), tool_calls=[ToolCall(key, str(item.get("name", "")), _json_arguments(item.get("arguments") or {})) for key, item in calls.items() if item.get("name")])
                yield AIStreamEvent("done", response=final)
                continue
            if self.protocol == "chat":
                choices = value.get("choices")
                delta = choices[0].get("delta", {}) if isinstance(choices, list) and choices and isinstance(choices[0], Mapping) else {}
                fragment = _content_text(delta.get("content"))
                if fragment:
                    text_chunks.append(fragment)
                    yield AIStreamEvent("text_delta", text=fragment)
                raw_calls = delta.get("tool_calls")
                if isinstance(raw_calls, list):
                    for raw in raw_calls:
                        if not isinstance(raw, Mapping):
                            continue
                        raw_id = raw.get("id")
                        raw_index = raw.get("index")
                        index_key = str(raw_index) if raw_index is not None else None
                        if index_key is not None and index_key in call_indices:
                            key = call_indices[index_key]
                        else:
                            key = str(raw_id) if raw_id else index_key if index_key is not None else str(len(calls))
                            if index_key is not None:
                                call_indices[index_key] = key
                        function = raw.get("function") if isinstance(raw.get("function"), Mapping) else {}
                        row = calls.setdefault(key, {"name": "", "arguments": ""})
                        if isinstance(function.get("name"), str):
                            row["name"] = function["name"]
                        if isinstance(function.get("arguments"), str):
                            row["arguments"] += function["arguments"]
            else:
                event_type = str(value.get("type", ""))
                if event_type.endswith("output_text.delta") and isinstance(value.get("delta"), str):
                    text_chunks.append(value["delta"])
                    yield AIStreamEvent("text_delta", text=value["delta"])
                elif event_type.endswith("output_item.added") and isinstance(value.get("item"), Mapping):
                    item = value["item"]
                    if item.get("type") == "function_call":
                        key = str(item.get("call_id") or item.get("id") or len(calls))
                        calls[key] = {"name": item.get("name", ""), "arguments": item.get("arguments", "")}
                elif event_type.endswith("function_call_arguments.delta"):
                    key = str(value.get("call_id") or value.get("item_id") or len(calls))
                    row = calls.setdefault(key, {"name": value.get("name", ""), "arguments": ""})
                    if isinstance(value.get("delta"), str):
                        row["arguments"] = str(row.get("arguments", "")) + value["delta"]
                elif event_type.endswith("function_call_arguments.done"):
                    key = str(value.get("call_id") or value.get("item_id") or len(calls))
                    row = calls.setdefault(key, {"name": "", "arguments": ""})
                    if isinstance(value.get("name"), str):
                        row["name"] = value["name"]
                    if isinstance(value.get("arguments"), str):
                        row["arguments"] = value["arguments"]
        await producer
        if not saw_done:
            final = AIResponse(text="".join(text_chunks), tool_calls=[ToolCall(key, str(item.get("name", "")), _json_arguments(item.get("arguments") or {})) for key, item in calls.items() if item.get("name")])
            yield AIStreamEvent("done", response=final)


class ScriptedProvider:
    """Deterministic provider used by examples, tests, and local demos."""

    def __init__(self, responses: Iterable[Union[AIResponse, Mapping[str, Any]]]) -> None:
        self.responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    async def complete(self, messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]], *, model: Optional[str] = None, temperature: Optional[float] = None) -> AIResponse:
        self.calls.append({"messages": [dict(item) for item in messages], "tools": list(tools), "model": model})
        if not self.responses:
            return AIResponse(text="没有更多模拟响应")
        value = self.responses.pop(0)
        if isinstance(value, AIResponse):
            return value
        calls = []
        raw_calls = value.get("tool_calls", []) if isinstance(value, Mapping) else []
        for index, item in enumerate(raw_calls if isinstance(raw_calls, list) else []):
            if isinstance(item, ToolCall):
                calls.append(item)
            elif isinstance(item, Mapping):
                calls.append(ToolCall(str(item.get("id") or "call-%d" % index), str(item.get("name")), _json_arguments(item.get("arguments", {}))))
        return AIResponse(text=str(value.get("text", "")) if isinstance(value, Mapping) else "", tool_calls=calls, raw=dict(value) if isinstance(value, Mapping) else None)

    async def stream(self, messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]], *, model: Optional[str] = None, temperature: Optional[float] = None) -> AsyncIterator[AIStreamEvent]:
        response = await self.complete(messages, tools, model=model, temperature=temperature)
        if response.text:
            yield AIStreamEvent("text_delta", text=response.text)
        yield AIStreamEvent("done", response=response)


class JsonlSubprocessProvider:
    """Adapter for a local CLI that prints one normalized JSON response.

    Each invocation receives a JSON request on stdin and may print multiple
    JSONL rows.  Rows with ``type=tool_call`` are accumulated; the last
    ``type=message``/``response`` row supplies assistant text.
    """

    def __init__(self, command: Sequence[str], *, cwd: Optional[str] = None, env: Optional[Mapping[str, str]] = None, timeout: float = 300.0) -> None:
        if not command:
            raise ValueError("command is required")
        self.command = list(command)
        self.cwd = cwd
        self.env = dict(env or {})
        if not isinstance(timeout, (int, float)) or not math.isfinite(float(timeout)) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number")
        self.timeout = max(0.1, float(timeout))

    def __repr__(self) -> str:
        return "JsonlSubprocessProvider(command=%r, cwd=%r, timeout=%r)" % (self.command, self.cwd, self.timeout)

    def _run(self, request: Mapping[str, Any]) -> AIResponse:
        environment = os.environ.copy()
        environment.update(self.env)
        try:
            completed = subprocess.run(self.command, input=(json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.cwd, env=environment, timeout=self.timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AIProviderError("AI subprocess failed: %s" % str(exc)[:1000]) from exc
        if completed.returncode != 0:
            # stderr may contain credentials or host paths; keep it out of
            # model-visible events. Hosts can inspect their own process logs.
            raise AIProviderError("AI subprocess exited with %d" % completed.returncode)
        text = ""
        calls: List[ToolCall] = []
        for line in completed.stdout.decode("utf-8", "replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, Mapping):
                continue
            row_type = row.get("type")
            if row_type == "tool_call":
                calls.append(ToolCall(str(row.get("id") or "call-%d" % len(calls)), str(row.get("name")), _json_arguments(row.get("arguments", {}))))
            elif row_type in {"message", "response"}:
                text = _content_text(row.get("text", row.get("content", "")))
        return AIResponse(text=text, tool_calls=calls)

    async def complete(self, messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]], *, model: Optional[str] = None, temperature: Optional[float] = None) -> AIResponse:
        return await asyncio.to_thread(self._run, {"messages": [dict(item) for item in messages], "tools": list(tools), "model": model, "temperature": temperature})

    async def stream(self, messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]], *, model: Optional[str] = None, temperature: Optional[float] = None) -> AsyncIterator[AIStreamEvent]:
        response = await self.complete(messages, tools, model=model, temperature=temperature)
        if response.text:
            yield AIStreamEvent("text_delta", text=response.text)
        yield AIStreamEvent("done", response=response)


class CodexCLIProvider(JsonlSubprocessProvider):
    """Best-effort adapter for the JSONL emitted by Codex CLI.

    The CLI evolves independently from Loomcraft, so the adapter intentionally
    accepts both the current item/completed envelope and the compact normalized
    rows accepted by JsonlSubprocessProvider. For installations using a
    persistent app-server, implement AIProvider around that process instead.
    """

    def __init__(self, codex_bin: str = "codex", *, cwd: Optional[str] = None, sandbox: str = "workspace-write", timeout: float = 300.0, extra_args: Optional[Sequence[str]] = None) -> None:
        if not codex_bin or any(char in codex_bin for char in "\x00\r\n"):
            raise ValueError("codex_bin is invalid")
        if sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
            raise ValueError("sandbox must be read-only, workspace-write, or danger-full-access")
        command = [codex_bin, "exec", "--json", "--sandbox", sandbox]
        command.extend(list(extra_args or []))
        command.append("-")
        super().__init__(command, cwd=cwd, timeout=timeout)

    def _run(self, request: Mapping[str, Any]) -> AIResponse:
        # Keep the generic parser's process handling, then parse a second pass
        # for Codex's nested item/completed rows if the compact parser found no
        # result. This method deliberately does not expose stderr or env data.
        environment = os.environ.copy()
        try:
            completed = subprocess.run(self.command, input=(json.dumps(request, ensure_ascii=False) + "\n").encode("utf-8"), stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=self.cwd, env=environment, timeout=self.timeout, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AIProviderError("Codex CLI failed: %s" % str(exc)[:1000]) from exc
        if completed.returncode != 0:
            raise AIProviderError("Codex CLI exited with %d" % completed.returncode)
        text = ""
        calls: List[ToolCall] = []
        for line in completed.stdout.decode("utf-8", "replace").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if not isinstance(row, Mapping):
                continue
            item = row.get("item") if isinstance(row.get("item"), Mapping) else row
            item_type = item.get("type")
            if item_type in {"function_call", "tool_call", "item/tool/call"}:
                name = item.get("name") or item.get("tool")
                if isinstance(name, str):
                    calls.append(ToolCall(str(item.get("call_id") or item.get("id") or "call-%d" % len(calls)), name, _json_arguments(item.get("arguments", item.get("input", {})))))
            elif item_type in {"agent_message", "message", "output_text", "item/completed"}:
                candidate = item.get("text") or item.get("content") or item.get("message")
                if isinstance(candidate, str):
                    text = candidate
                elif isinstance(candidate, list):
                    text = _content_text(candidate)
        return AIResponse(text=text, tool_calls=calls)


class ResponsesAPIProvider(OpenAICompatibleProvider):
    """Convenience provider pinned to the OpenAI Responses protocol."""

    def __init__(self, api_key: str, *, base_url: str = "https://api.openai.com/v1", model: str = "gpt-4.1-mini", timeout: float = 120.0, allow_insecure_http: bool = False, extra_params: Optional[Mapping[str, Any]] = None) -> None:
        super().__init__(api_key, base_url=base_url, model=model, protocol="responses", timeout=timeout, allow_insecure_http=allow_insecure_http, extra_params=extra_params)


def provider_from_env(prefix: str = "LOOMCRAFT_") -> OpenAICompatibleProvider:
    """Build an OpenAI-compatible provider from explicit host environment vars.

    Supported variables are ``<prefix>API_KEY``, ``BASE_URL``, ``MODEL``,
    ``PROTOCOL`` (chat/responses), ``TIMEOUT`` and ``ALLOW_INSECURE_HTTP``.
    The prefix is configurable so a host can keep multiple model chains
    separate; no global network or process configuration is changed.
    """
    key = os.environ.get(prefix + "API_KEY", "")
    if not key:
        raise ValueError("%sAPI_KEY is not configured" % prefix)
    protocol = os.environ.get(prefix + "PROTOCOL", "chat").strip().lower()
    return OpenAICompatibleProvider(
        key,
        base_url=os.environ.get(prefix + "BASE_URL", "https://api.openai.com/v1"),
        model=os.environ.get(prefix + "MODEL", "gpt-4.1-mini"),
        protocol=protocol,
        timeout=float(os.environ.get(prefix + "TIMEOUT", "120")),
        allow_insecure_http=os.environ.get(prefix + "ALLOW_INSECURE_HTTP", "false").strip().lower() == "true",
    )


@dataclass
class AgentRunResult:
    status: str
    text: str
    rounds: int
    tool_results: List[Dict[str, Any]] = field(default_factory=list)
    error: Optional[str] = None


class PlannerAgent:
    """Run an AI turn and route native tool calls through ``ToolBroker``."""

    def __init__(
        self,
        provider: AIProvider,
        broker: ToolBroker,
        *,
        model: Optional[str] = None,
        system_prompt: Optional[str] = None,
        max_rounds: int = 16,
        temperature: Optional[float] = None,
        stream: bool = False,
        extra_tools: Optional[Sequence[Mapping[str, Any]]] = None,
    ) -> None:
        self.provider = provider
        self.broker = broker
        self.model = model
        self.system_prompt = system_prompt or (
            "You are a planning agent. Inspect context before acting. Publish a complete "
            "versioned DAG with publish_plan, then execute only steps authorized by that "
            "plan. Never invent capability identifiers. Keep scope and evidence explicit."
        )
        self.max_rounds = max(1, int(max_rounds))
        self.temperature = temperature
        self.stream = stream
        self.extra_tools = [dict(tool) for tool in (extra_tools or [])]

    async def _complete(self, messages: Sequence[Mapping[str, Any]], tools: Sequence[Mapping[str, Any]], item_id: str) -> AIResponse:
        if not self.stream or not callable(getattr(self.provider, "stream", None)):
            return await self.provider.complete(messages, tools, model=self.model, temperature=self.temperature)
        final: Optional[AIResponse] = None
        async for item in self.provider.stream(messages, tools, model=self.model, temperature=self.temperature):
            if item.text:
                self.broker._emit("message_delta", {"delta": item.text, "item_id": item_id})
            if item.response is not None:
                final = item.response
        return final or AIResponse()

    def _finish(self, result: AgentRunResult) -> AgentRunResult:
        session = getattr(self.broker, "session", None)
        if session is not None and callable(getattr(session, "update_meta", None)):
            try:
                session.update_meta(
                    status=("waiting_inputs" if result.status == "waiting_inputs" else "idle"),
                    last_turn_status=result.status,
                )
            except Exception:
                pass
        return result

    async def run(self, message: str, *, prior_messages: Optional[Sequence[Mapping[str, Any]]] = None) -> AgentRunResult:
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must be non-empty")
        self.broker.begin_turn()
        session = getattr(self.broker, "session", None)
        get_session = getattr(getattr(self.broker, "store", None), "get_session", None)
        update_session = getattr(getattr(self.broker, "store", None), "update_session", None)
        if session is not None and callable(getattr(session, "meta", None)):
            meta = session.meta()
            turns = int(meta.get("turns", 0)) if isinstance(meta, Mapping) and isinstance(meta.get("turns", 0), int) else 0
            try:
                session.update_meta(turns=turns + 1, status="running")
            except Exception:
                pass
        elif callable(get_session) and callable(update_session):
            meta = get_session(getattr(self.broker, "session_id", ""))
            turns = int(meta.get("turns", 0)) if isinstance(meta, Mapping) and isinstance(meta.get("turns", 0), int) else 0
            try:
                update_session(getattr(self.broker, "session_id", ""), turns=turns + 1, status="running")
            except Exception:
                pass
        messages: List[Dict[str, Any]] = [{"role": "system", "content": self.system_prompt}]
        if prior_messages:
            messages.extend(dict(item) for item in prior_messages)
        messages.append({"role": "user", "content": message})
        append_message = getattr(getattr(self.broker, "store", None), "append_message", None)
        if callable(append_message):
            try:
                append_message(getattr(self.broker, "session_id", ""), "user", message)
            except Exception:
                pass
        native_specs = dynamic_tool_specs()
        native_names = {
            str(item.get("name"))
            or str((item.get("function") or {}).get("name"))
            for item in native_specs
            if isinstance(item, Mapping)
        }
        safe_extra = []
        for item in self.extra_tools:
            function = item.get("function") if isinstance(item.get("function"), Mapping) else {}
            name = item.get("name") or function.get("name")
            if str(name) not in native_names:
                safe_extra.append(item)
        tool_specs = openai_tool_specs([*native_specs, *safe_extra])
        results: List[Dict[str, Any]] = []
        final_text = ""
        for round_index in range(1, self.max_rounds + 1):
            try:
                item_id = "agent-round-%d" % round_index
                response = await self._complete(messages, tool_specs, item_id)
            except Exception as exc:
                self.broker._emit("error", {"message": "AI provider failed: %s" % str(exc)[:1000]})
                return self._finish(AgentRunResult("error", final_text, round_index, results, str(exc)))
            if response.text:
                final_text = response.text
                self.broker._emit("message", {"text": response.text, "item_id": item_id})
                if callable(append_message):
                    try:
                        append_message(getattr(self.broker, "session_id", ""), "assistant", response.text)
                    except Exception:
                        pass
            if not response.tool_calls:
                self.broker._emit("done", {"status": "completed"})
                return self._finish(AgentRunResult("completed", final_text, round_index, results))
            assistant_call_rows = []
            for call in response.tool_calls:
                assistant_call_rows.append({"id": call.id, "type": "function", "function": {"name": call.name, "arguments": json.dumps(call.arguments, ensure_ascii=False, allow_nan=False)}})
            messages.append({"role": "assistant", "content": response.text or None, "tool_calls": assistant_call_rows})
            for call in response.tool_calls:
                self.broker._emit("tool_call", {"item_id": call.id, "tool": call.name, "arguments": call.arguments})
                dispatched = await self.broker.dispatch(call.name, call.arguments)
                result = dispatched.to_dict() if hasattr(dispatched, "to_dict") else dict(dispatched)
                results.append({"call_id": call.id, "tool": call.name, "response": result})
                self.broker._emit("tool_result", {"item_id": call.id, "tool": call.name, "ok": result.get("ok", False), "error": result.get("error"), "error_code": result.get("error_code")})
                messages.append({"role": "tool", "tool_call_id": call.id, "name": call.name, "content": json.dumps(result, ensure_ascii=False, allow_nan=False)})
                if call.name.replace("-", "_") == "request_inputs" and result.get("ok"):
                    self.broker._emit("done", {"status": "waiting_inputs"})
                    return self._finish(AgentRunResult("waiting_inputs", final_text, round_index, results))
        error = "AI tool loop exceeded max_rounds=%d" % self.max_rounds
        self.broker._emit("error", {"message": error})
        return self._finish(AgentRunResult("error", final_text, self.max_rounds, results, error))


__all__ = [
    "AIProvider",
    "AIProviderError",
    "AIResponse",
    "AIStreamEvent",
    "AgentRunResult",
    "CodexCLIProvider",
    "JsonlSubprocessProvider",
    "OpenAICompatibleProvider",
    "PlannerAgent",
    "ResponsesAPIProvider",
    "ScriptedProvider",
    "StreamingAIProvider",
    "ToolCall",
    "dynamic_tool_specs",
    "openai_tool_specs",
    "parse_chat_response",
    "parse_responses_response",
    "provider_from_env",
]
