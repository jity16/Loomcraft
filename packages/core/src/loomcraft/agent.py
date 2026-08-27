"""Driving a model against the broker.

LoomCraft does not care which model you use — the broker validates tool calls
regardless of who made them.  This module supplies the loop that connects the
two, plus two implementations:

:class:`AnthropicAgent`
    A production loop against the Claude Messages API: streaming, adaptive
    thinking, parallel tool execution, and a bounded iteration count.

:class:`ScriptedAgent`
    A deterministic agent that replays a fixed list of tool calls. Examples,
    tests, and CI use it so the whole engine is exercisable with no API key and
    no network.

Both satisfy the same :class:`Agent` protocol, so a host can swap them per
environment.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Iterable, Mapping, Protocol, Sequence

from .broker import ToolBroker, ToolResponse
from .tools import SYSTEM_PROMPT, Dialect, ToolSpec, to_dialect, tool_specs

logger = logging.getLogger("loomcraft.agent")

DEFAULT_MODEL = "claude-opus-5"
DEFAULT_MAX_TOKENS = 16_000
DEFAULT_MAX_ITERATIONS = 24

EventSink = Callable[[str, Mapping[str, Any]], Any]


@dataclass
class ToolCall:
    """One model-requested tool invocation."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class TurnResult:
    """What one agent turn produced."""

    text: str = ""
    stop_reason: str | None = None
    iterations: int = 0
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResponse] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class Agent(Protocol):
    """Anything that can drive one turn against a broker."""

    async def run_turn(
        self,
        broker: ToolBroker,
        message: str,
        *,
        history: Sequence[Mapping[str, Any]] | None = ...,
        on_event: EventSink | None = ...,
    ) -> TurnResult: ...


# ── Shared plumbing ─────────────────────────────────────────────────────────


async def execute_tool_calls(
    broker: ToolBroker,
    calls: Iterable[ToolCall],
    *,
    on_event: EventSink | None = None,
) -> list[tuple[ToolCall, ToolResponse]]:
    """Run a batch of tool calls concurrently and emit UI events for each.

    A model may request several tools in one assistant message; running them
    concurrently keeps the turn responsive. The broker still serialises anything
    that must not overlap — a second ``run_capability`` is refused, not queued.
    """
    batch = list(calls)
    if not batch:
        return []

    def emit(name: str, data: Mapping[str, Any]) -> None:
        if on_event is not None:
            try:
                on_event(name, data)
            except Exception:  # noqa: BLE001 - a UI sink must not break the turn
                logger.debug("event sink raised for %s", name, exc_info=True)

    for call in batch:
        emit(
            "tool_call",
            {
                "item_id": call.id,
                "tool": call.name,
                "step_id": call.arguments.get("step_id"),
            },
        )

    async def one(call: ToolCall) -> tuple[ToolCall, ToolResponse]:
        response = await broker.dispatch(call.name, call.arguments)
        emit(
            "tool_result",
            {
                "item_id": call.id,
                "tool": call.name,
                "step_id": call.arguments.get("step_id"),
                "ok": response.ok,
                "exit_code": 0 if response.ok else 1,
                "error": response.error,
                "error_code": response.error_code,
            },
        )
        return call, response

    return list(await asyncio.gather(*(one(call) for call in batch)))


# ── Anthropic ───────────────────────────────────────────────────────────────


class AnthropicAgent:
    """Drives Claude against a :class:`~loomcraft.broker.ToolBroker`.

    Uses the streaming Messages API so long planning turns cannot hit an HTTP
    timeout, and leaves adaptive thinking on — planning a DAG is exactly the
    multi-step reasoning it helps with.
    """

    def __init__(
        self,
        *,
        client: Any | None = None,
        model: str = DEFAULT_MODEL,
        system: str = SYSTEM_PROMPT,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        effort: str | None = "high",
        thinking: Mapping[str, Any] | None = None,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        tools: Sequence[ToolSpec] | None = None,
        extra_body: Mapping[str, Any] | None = None,
    ) -> None:
        self._client = client
        self.model = model
        self.system = system
        self.max_tokens = max_tokens
        self.effort = effort
        # Adaptive thinking with summarized display: the UI can show why the
        # agent chose a plan shape. Pass ``{"type": "disabled"}`` to turn it off.
        self.thinking: dict[str, Any] = dict(
            thinking or {"type": "adaptive", "display": "summarized"}
        )
        self.max_iterations = max_iterations
        self._specs = list(tools) if tools is not None else tool_specs()
        self.extra_body = dict(extra_body or {})

    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                import anthropic  # noqa: PLC0415 - optional dependency
            except ImportError as exc:  # pragma: no cover - import guard
                raise RuntimeError(
                    "AnthropicAgent needs the `anthropic` package: "
                    "pip install 'loomcraft[anthropic]'"
                ) from exc
            # Resolves ANTHROPIC_API_KEY, ANTHROPIC_AUTH_TOKEN, or an
            # `ant auth login` profile — no key needs to be passed explicitly.
            self._client = anthropic.AsyncAnthropic()
        return self._client

    def tool_definitions(self, dialect: Dialect = "anthropic") -> list[dict[str, Any]]:
        return to_dialect(self._specs, dialect)

    async def run_turn(
        self,
        broker: ToolBroker,
        message: str,
        *,
        history: Sequence[Mapping[str, Any]] | None = None,
        on_event: EventSink | None = None,
    ) -> TurnResult:
        broker.begin_turn()
        messages: list[dict[str, Any]] = [dict(row) for row in (history or [])]
        messages.append({"role": "user", "content": message})

        result = TurnResult(messages=messages)
        request: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "system": self.system,
            "tools": self.tool_definitions("anthropic"),
            "thinking": self.thinking,
            **self.extra_body,
        }
        if self.effort:
            request["output_config"] = {"effort": self.effort, **request.get("output_config", {})}

        try:
            for iteration in range(1, self.max_iterations + 1):
                result.iterations = iteration
                async with self.client.messages.stream(
                    **request, messages=messages
                ) as stream:
                    async for event in stream:
                        if (
                            on_event is not None
                            and getattr(event, "type", None) == "content_block_delta"
                            and getattr(event.delta, "type", None) == "text_delta"
                        ):
                            on_event(
                                "message_delta",
                                {"item_id": f"msg-{iteration}", "delta": event.delta.text},
                            )
                    response = await stream.get_final_message()

                for key in ("input_tokens", "output_tokens"):
                    value = getattr(response.usage, key, None)
                    if isinstance(value, int):
                        result.usage[key] = result.usage.get(key, 0) + value

                text = "".join(
                    block.text for block in response.content if block.type == "text"
                )
                if text.strip():
                    result.text = text
                    if on_event is not None:
                        on_event(
                            "message",
                            {"item_id": f"msg-{iteration}", "text": text},
                        )

                result.stop_reason = response.stop_reason

                # A safety decline arrives as a normal 200 with an empty or
                # partial body — check before reading content as an answer.
                if response.stop_reason == "refusal":
                    details = getattr(response, "stop_details", None)
                    category = getattr(details, "category", None)
                    result.error = f"model declined the request (category={category})"
                    if on_event is not None:
                        on_event("error", {"message": result.error})
                    return result

                calls = [
                    ToolCall(id=block.id, name=block.name, arguments=dict(block.input))
                    for block in response.content
                    if block.type == "tool_use"
                ]
                if not calls:
                    return result

                # Preserve the full assistant content (thinking blocks included)
                # — trimming it breaks signature validation on the next request.
                messages.append({"role": "assistant", "content": response.content})
                executed = await execute_tool_calls(broker, calls, on_event=on_event)
                result.tool_calls.extend(call for call, _ in executed)
                result.tool_results.extend(response for _, response in executed)
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": call.id,
                                "content": response.to_tool_result_text(),
                                "is_error": not response.ok,
                            }
                            for call, response in executed
                        ],
                    }
                )

            result.error = (
                f"agent exceeded {self.max_iterations} tool iterations without finishing"
            )
            if on_event is not None:
                on_event("error", {"message": result.error})
            return result
        except asyncio.CancelledError:
            await broker.close()
            raise
        except Exception as exc:  # noqa: BLE001 - stable turn boundary
            logger.exception("anthropic agent turn failed")
            result.error = f"{type(exc).__name__}: {exc}"
            if on_event is not None:
                on_event("error", {"message": result.error})
            return result


# ── OpenAI-compatible ───────────────────────────────────────────────────────


class OpenAICompatibleAgent:
    """Drives any OpenAI-style chat-completions endpoint.

    Included so the tool surface is demonstrably provider-neutral. The loop is
    the same shape as :class:`AnthropicAgent`; only the wire format differs.
    """

    def __init__(
        self,
        client: Any,
        *,
        model: str,
        system: str = SYSTEM_PROMPT,
        max_iterations: int = DEFAULT_MAX_ITERATIONS,
        tools: Sequence[ToolSpec] | None = None,
        extra_body: Mapping[str, Any] | None = None,
    ) -> None:
        self.client = client
        self.model = model
        self.system = system
        self.max_iterations = max_iterations
        self._specs = list(tools) if tools is not None else tool_specs()
        self.extra_body = dict(extra_body or {})

    async def run_turn(
        self,
        broker: ToolBroker,
        message: str,
        *,
        history: Sequence[Mapping[str, Any]] | None = None,
        on_event: EventSink | None = None,
    ) -> TurnResult:
        broker.begin_turn()
        messages: list[dict[str, Any]] = [{"role": "system", "content": self.system}]
        messages.extend(dict(row) for row in (history or []))
        messages.append({"role": "user", "content": message})
        result = TurnResult(messages=messages)

        try:
            for iteration in range(1, self.max_iterations + 1):
                result.iterations = iteration
                response = await self.client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    tools=to_dialect(self._specs, "openai"),
                    **self.extra_body,
                )
                choice = response.choices[0]
                assistant = choice.message
                if assistant.content:
                    result.text = assistant.content
                    if on_event is not None:
                        on_event(
                            "message",
                            {"item_id": f"msg-{iteration}", "text": assistant.content},
                        )
                result.stop_reason = choice.finish_reason

                raw_calls = getattr(assistant, "tool_calls", None) or []
                if not raw_calls:
                    return result

                calls = [
                    ToolCall(
                        id=item.id,
                        name=item.function.name,
                        arguments=json.loads(item.function.arguments or "{}"),
                    )
                    for item in raw_calls
                ]
                messages.append(assistant.model_dump())
                executed = await execute_tool_calls(broker, calls, on_event=on_event)
                result.tool_calls.extend(call for call, _ in executed)
                result.tool_results.extend(response for _, response in executed)
                for call, tool_response in executed:
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": tool_response.to_tool_result_text(),
                        }
                    )

            result.error = (
                f"agent exceeded {self.max_iterations} tool iterations without finishing"
            )
            return result
        except asyncio.CancelledError:
            await broker.close()
            raise
        except Exception as exc:  # noqa: BLE001
            logger.exception("openai-compatible agent turn failed")
            result.error = f"{type(exc).__name__}: {exc}"
            return result


# ── Scripted (no network) ───────────────────────────────────────────────────

ScriptStep = tuple[str, Mapping[str, Any]]
ScriptFn = Callable[[list[ToolResponse]], Awaitable[Sequence[ScriptStep]] | Sequence[ScriptStep]]


class ScriptedAgent:
    """Replays a fixed sequence of tool calls — no model, no network, no key.

    Two forms:

    * a static list of ``(tool_name, arguments)`` pairs, or
    * a callable that receives the responses so far and returns the next batch,
      which is enough to script "if step X failed, publish revision 2".

    Every call still goes through the broker, so a scripted run exercises the
    same validation, execution, and event path as a real one.
    """

    def __init__(
        self,
        script: Sequence[ScriptStep] | ScriptFn,
        *,
        final_text: str = "",
    ) -> None:
        self._script = script
        self.final_text = final_text

    async def run_turn(
        self,
        broker: ToolBroker,
        message: str,
        *,
        history: Sequence[Mapping[str, Any]] | None = None,
        on_event: EventSink | None = None,
    ) -> TurnResult:
        broker.begin_turn()
        result = TurnResult()

        if callable(self._script):
            batches: list[Sequence[ScriptStep]] = []
            responses: list[ToolResponse] = []
            for _ in range(DEFAULT_MAX_ITERATIONS):
                produced = self._script(responses)
                if asyncio.iscoroutine(produced):
                    produced = await produced  # type: ignore[assignment]
                steps = list(produced)  # type: ignore[arg-type]
                if not steps:
                    break
                batches.append(steps)
                result.iterations += 1
                executed = await self._run_batch(broker, steps, result, on_event)
                responses.extend(response for _, response in executed)
        else:
            for index, (name, arguments) in enumerate(self._script, start=1):
                result.iterations = index
                await self._run_batch(broker, [(name, arguments)], result, on_event)

        result.text = self.final_text
        if self.final_text and on_event is not None:
            on_event("message", {"item_id": "msg-final", "text": self.final_text})
        result.stop_reason = "end_turn"
        return result

    async def _run_batch(
        self,
        broker: ToolBroker,
        steps: Sequence[ScriptStep],
        result: TurnResult,
        on_event: EventSink | None,
    ) -> list[tuple[ToolCall, ToolResponse]]:
        calls = [
            ToolCall(
                id=f"scripted-{len(result.tool_calls) + offset + 1}",
                name=name,
                arguments=dict(arguments),
            )
            for offset, (name, arguments) in enumerate(steps)
        ]
        executed = await execute_tool_calls(broker, calls, on_event=on_event)
        result.tool_calls.extend(call for call, _ in executed)
        result.tool_results.extend(response for _, response in executed)
        return executed


__all__ = [
    "Agent",
    "AnthropicAgent",
    "DEFAULT_MAX_ITERATIONS",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MODEL",
    "EventSink",
    "OpenAICompatibleAgent",
    "ScriptedAgent",
    "ToolCall",
    "TurnResult",
    "execute_tool_calls",
]
