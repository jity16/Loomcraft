"""Streaming and subprocess agent providers.

Both are exercised without a network: the streaming case drives a fake client
that emits realistically fragmented chunks, and the subprocess case runs a real
child process speaking the documented JSONL protocol.
"""

from __future__ import annotations

import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

import loomcraft as lc

SIMPLE_PLAN = {
    "goal": "demo",
    "revision": 1,
    "steps": [{"id": "a", "title": "Answer", "kind": "answer"}],
}


class _Obj:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _chunk(content=None, tool_calls=None, finish=None):
    return _Obj(
        choices=[
            _Obj(
                delta=_Obj(content=content, tool_calls=tool_calls),
                finish_reason=finish,
            )
        ]
    )


class _Stream:
    def __init__(self, chunks):
        self._chunks = chunks

    def __aiter__(self):
        async def generate():
            for chunk in self._chunks:
                yield chunk

        return generate()


class _Completions:
    """Emits one fragmented tool call, then a plain final message."""

    def __init__(self):
        self.calls = 0
        self.last_kwargs: dict = {}

    async def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        if self.calls == 1:
            head = json.dumps({"plan": SIMPLE_PLAN})[:20]
            tail = json.dumps({"plan": SIMPLE_PLAN})[20:]
            return _Stream(
                [
                    _chunk(content="Publishing "),
                    _chunk(content="the plan."),
                    _chunk(
                        tool_calls=[
                            _Obj(
                                index=0,
                                id="call-1",
                                function=_Obj(name="publish_plan", arguments=head),
                            )
                        ]
                    ),
                    _chunk(
                        tool_calls=[
                            _Obj(index=0, id=None, function=_Obj(name=None, arguments=tail))
                        ]
                    ),
                    _chunk(finish="tool_calls"),
                ]
            )
        return _Stream([_chunk(content="published"), _chunk(finish="stop")])


class _Client:
    def __init__(self):
        self.chat = _Obj(completions=_Completions())


class ProviderCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = lc.SessionStore(Path(self._tmp.name) / "sessions", in_memory_events=True)
        self.session = self.store.create("test")
        self.registry = lc.Registry()
        self.broker = lc.ToolBroker(self.session, self.registry)

    async def asyncTearDown(self):
        await self.broker.close()
        self._tmp.cleanup()


class TestStreaming(ProviderCase):
    async def test_deltas_are_emitted_and_fragments_reassembled(self):
        client = _Client()
        agent = lc.OpenAICompatibleAgent(client, model="test-model", stream=True)
        events: list[tuple[str, dict]] = []

        result = await agent.run_turn(
            self.broker, "go", on_event=lambda name, data: events.append((name, data))
        )

        self.assertIsNone(result.error, result.error)
        # Deltas are grouped by message item, one item per iteration.
        first_message = [
            data["text"]
            for name, data in events
            if name == "message_delta" and data["item_id"] == "msg-1"
        ]
        self.assertEqual(first_message, ["Publishing ", "the plan."])

        # The tool arguments arrived split mid-JSON; they must be reassembled
        # before parsing or the call is lost.
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(result.tool_calls[0].name, "publish_plan")
        self.assertEqual(result.tool_calls[0].arguments["plan"]["goal"], "demo")
        self.assertIsNotNone(self.session.current_plan())
        self.assertEqual(result.text, "published")

    async def test_streaming_requests_the_stream(self):
        client = _Client()
        agent = lc.OpenAICompatibleAgent(client, model="test-model", stream=True)
        await agent.run_turn(self.broker, "go")
        self.assertTrue(client.chat.completions.last_kwargs.get("stream"))

    async def test_non_streaming_remains_the_default(self):
        client = _Client()
        agent = lc.OpenAICompatibleAgent(client, model="test-model")
        self.assertFalse(agent.stream)


def _write_runner(directory: Path, body: str) -> Path:
    path = directory / "runner.py"
    path.write_text(textwrap.dedent(body), encoding="utf-8")
    return path


class TestSubprocessAgent(ProviderCase):
    async def test_a_child_process_can_drive_a_full_turn(self):
        runner = _write_runner(
            Path(self._tmp.name),
            f"""
            import json, sys
            request = json.loads(sys.stdin.readline())
            turn = sum(1 for m in request["messages"] if m.get("role") == "tool")
            out = sys.stdout
            if turn == 0:
                out.write(json.dumps({{"type": "delta", "text": "planning"}}) + "\\n")
                out.write(json.dumps({{
                    "type": "tool_call", "id": "c1", "name": "publish_plan",
                    "arguments": {{"plan": {json.dumps(SIMPLE_PLAN)}}},
                }}) + "\\n")
                out.write(json.dumps({{"type": "done", "stop_reason": "tool_use"}}) + "\\n")
            else:
                tools = len(request["tools"])
                out.write(json.dumps({{
                    "type": "done", "text": f"saw {{tools}} tools",
                    "stop_reason": "end_turn",
                }}) + "\\n")
            out.flush()
            """,
        )
        agent = lc.SubprocessAgent([sys.executable, str(runner)])
        events: list[str] = []
        result = await agent.run_turn(
            self.broker, "plan it", on_event=lambda name, data: events.append(name)
        )

        self.assertIsNone(result.error, result.error)
        self.assertEqual(result.iterations, 2)
        self.assertIn("message_delta", events)
        self.assertIsNotNone(self.session.current_plan())
        self.assertEqual(result.text, f"saw {len(lc.tool_specs())} tools")

    async def test_a_noisy_runner_does_not_break_the_turn(self):
        runner = _write_runner(
            Path(self._tmp.name),
            """
            import json, sys
            sys.stdin.readline()
            print("loading model weights...")          # not JSON
            print(json.dumps({"type": "done", "text": "fine", "stop_reason": "end_turn"}))
            sys.stdout.flush()
            """,
        )
        agent = lc.SubprocessAgent([sys.executable, str(runner)])
        result = await agent.run_turn(self.broker, "go")
        self.assertIsNone(result.error, result.error)
        self.assertEqual(result.text, "fine")

    async def test_a_runner_error_line_becomes_a_turn_error(self):
        runner = _write_runner(
            Path(self._tmp.name),
            """
            import json, sys
            sys.stdin.readline()
            print(json.dumps({"type": "error", "message": "no model configured"}))
            sys.stdout.flush()
            """,
        )
        agent = lc.SubprocessAgent([sys.executable, str(runner)])
        result = await agent.run_turn(self.broker, "go")
        self.assertIsNotNone(result.error)
        self.assertIn("no model configured", result.error)

    async def test_a_crashing_runner_is_reported_not_hung(self):
        runner = _write_runner(
            Path(self._tmp.name),
            """
            import sys
            sys.stdin.readline()
            sys.stderr.write("boom\\n")
            raise SystemExit(3)
            """,
        )
        agent = lc.SubprocessAgent([sys.executable, str(runner)])
        result = await agent.run_turn(self.broker, "go")
        self.assertIsNotNone(result.error)
        self.assertIn("status 3", result.error)

    async def test_an_empty_argv_is_refused(self):
        with self.assertRaises(ValueError):
            lc.SubprocessAgent([])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
