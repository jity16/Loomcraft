import asyncio
import tempfile
import unittest
from pathlib import Path

from loomcraft import AIStreamEvent, AppServerBridge, InMemoryStore, LocalArtifactStore, OpenAICompatibleProvider, PlannerAgent, Registry, ResponsesAPIProvider, ScriptedProvider, StepResult, ToolBroker, dynamic_tool_specs, parse_chat_response, parse_responses_response, provider_from_env


class BrokerAiTest(unittest.IsolatedAsyncioTestCase):
    async def test_publish_and_tool_loop(self):
        registry = Registry()

        async def work(context):
            return StepResult(summary="done", output={"value": 1})

        registry.register_capability(id="work", name="work", handler=work)
        store = InMemoryStore()
        store.create_session("s")
        broker = ToolBroker("s", registry, store=store)
        provider = ScriptedProvider([
            {"tool_calls": [{"name": "publish_plan", "arguments": {"plan": {"goal": "x", "revision": 1, "steps": [{"id": "w", "title": "work", "kind": "capability", "capability": "work"}]}}}]},
            {"tool_calls": [{"name": "execute_plan", "arguments": {}}]},
            {"text": "finished"},
        ])
        result = await PlannerAgent(provider, broker).run("do it")
        self.assertEqual(result.status, "completed")
        self.assertEqual(store.get_current_plan("s")["steps"][0]["status"], "succeeded")
        self.assertEqual(store.get_session("s")["turns"], 1)
        self.assertIn("publish_plan", [call["function"]["name"] for call in provider.calls[0]["tools"]])

    async def test_agent_adds_extra_tools_without_shadowing_native_tools(self):
        provider = ScriptedProvider([{"text": "done"}])
        broker = ToolBroker("extra", Registry(), store=InMemoryStore())
        agent = PlannerAgent(provider, broker, extra_tools=[
            {"type": "function", "function": {"name": "host_lookup", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "publish_plan", "parameters": {"type": "object"}}},
        ])
        await agent.run("hello")
        names = [item["function"]["name"] for item in provider.calls[0]["tools"]]
        self.assertIn("host_lookup", names)
        self.assertEqual(names.count("publish_plan"), 1)

    async def test_extra_tool_handler_is_routed(self):
        store = InMemoryStore()
        broker = ToolBroker("extra-handler", Registry(), store=store, extra_tool_handlers={"host_lookup": lambda payload: {"found": payload.get("id")}})
        response = await broker.dispatch_dynamic_tool("host_lookup", {"id": "42"})
        self.assertTrue(response["ok"])
        self.assertEqual(response["result"]["found"], "42")

    async def test_action_limit_and_input_latch(self):
        registry = Registry()
        store = InMemoryStore()
        store.create_session("s")
        broker = ToolBroker("s", registry, store=store, max_actions=1)
        first = await broker.dispatch_dynamic_tool("session_context")
        second = await broker.dispatch_dynamic_tool("session_context")
        self.assertTrue(first["ok"])
        self.assertEqual(second["error_code"], "BROKER_ACTION_LIMIT_EXCEEDED")

    async def test_input_fulfillment_and_cancellation_are_audited(self):
        registry = Registry()
        store = InMemoryStore()
        store.create_session("input")
        broker = ToolBroker("input", registry, store=store)
        requested = await broker.dispatch_dynamic_tool("request_inputs", {"request": {"title": "Need CSV", "message": "upload", "requirements": [{"key": "table", "label": "Table", "description": "CSV", "required": True, "min_files": 1, "max_files": 1, "allowed_extensions": [".csv"], "field_hints": []}], "continue_prompt": "continue"}})
        request_id = requested["result"]["request"]["request_id"]
        allocation = broker.fulfill_inputs(request_id, [{"id": "u", "filename": "x.csv", "checksum": "c"}])
        self.assertEqual(allocation["table"], ["u"])
        requested2 = await broker.dispatch_dynamic_tool("request_inputs", {"request": {"title": "Need another", "message": "upload", "requirements": [{"key": "table", "label": "Table", "description": "CSV", "required": True, "min_files": 1, "max_files": 1, "allowed_extensions": [".csv"], "field_hints": []}], "continue_prompt": "continue"}})
        broker.cancel_input_request(requested2["result"]["request"]["request_id"])
        self.assertIn("input_cancelled", [event.event for event in store.read_events("input")])

    async def test_stop_latches_broker(self):
        registry = Registry()
        store = InMemoryStore()
        store.create_session("stop")
        broker = ToolBroker("stop", registry, store=store)
        await broker.stop()
        response = await broker.dispatch_dynamic_tool("session_context")
        self.assertEqual(response["error_code"], "BROKER_STOPPING")

    async def test_stop_waits_for_an_active_execution(self):
        registry = Registry()
        store = InMemoryStore()
        store.create_session("stop-run")
        async def slow(context):
            await asyncio.sleep(10)
        registry.register_capability(id="slow", name="slow", handler=slow)
        broker = ToolBroker("stop-run", registry, store=store)
        await broker.dispatch_dynamic_tool("publish_plan", {"plan": {"goal": "stop", "revision": 1, "steps": [{"id": "s", "title": "s", "kind": "capability", "capability": "slow"}]}})
        task = asyncio.create_task(broker.dispatch_dynamic_tool("execute_plan"))
        await asyncio.sleep(0.03)
        await broker.stop()
        result = await task
        self.assertEqual(result["result"]["status"], "cancelled")

    async def test_begin_turn_resets_action_budget(self):
        broker = ToolBroker("budget", Registry(), store=InMemoryStore(), max_actions=1)
        first = await broker.dispatch_dynamic_tool("session_context")
        blocked = await broker.dispatch_dynamic_tool("session_context")
        broker.begin_turn()
        second_turn = await broker.dispatch_dynamic_tool("session_context")
        self.assertTrue(first["ok"] and not blocked["ok"] and second_turn["ok"])

    def test_tool_contract_contains_source_actions(self):
        names = {item["name"] for item in dynamic_tool_specs()}
        self.assertTrue({"publish_plan", "run_capability", "run_workflow", "execute_plan", "request_inputs"} <= names)

    def test_provider_response_normalization(self):
        chat = parse_chat_response({"choices": [{"message": {"content": "hi", "tool_calls": [{"id": "c", "function": {"name": "publish_plan", "arguments": "{}"}}]}, "finish_reason": "tool_calls"}]})
        self.assertEqual(chat.text, "hi")
        self.assertEqual(chat.tool_calls[0].name, "publish_plan")
        responses = parse_responses_response({"output": [{"type": "message", "content": [{"type": "output_text", "text": "hello"}]}, {"type": "function_call", "call_id": "c2", "name": "session_context", "arguments": "{}"}]})
        self.assertEqual(responses.text, "hello")
        self.assertEqual(responses.tool_calls[0].id, "c2")

    def test_provider_rejects_unsafe_remote_http(self):
        with self.assertRaises(ValueError):
            OpenAICompatibleProvider("key", base_url="http://remote.example/v1")

    def test_responses_payload_uses_native_function_call_output_items(self):
        provider = ResponsesAPIProvider("key")
        payload = provider._request_payload([
            {"role": "system", "content": "rules"},
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "function": {"name": "session_context", "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "{\"ok\":true}"},
        ], [], None, None)
        self.assertEqual(payload["instructions"], "rules")
        self.assertEqual(payload["input"][1]["type"], "function_call")
        self.assertEqual(payload["input"][2]["type"], "function_call_output")

    def test_provider_normalizes_endpoint_suffix(self):
        provider = OpenAICompatibleProvider("key", base_url="https://example.com/v1/chat/completions")
        self.assertEqual(provider.base_url, "https://example.com/v1")
        self.assertNotIn("key", repr(provider))

    def test_provider_from_env_uses_explicit_prefix(self):
        import os
        old = {key: os.environ.get(key) for key in ("TEST_LOOM_API_KEY", "TEST_LOOM_PROTOCOL")}
        os.environ["TEST_LOOM_API_KEY"] = "key"
        os.environ["TEST_LOOM_PROTOCOL"] = "responses"
        try:
            provider = provider_from_env("TEST_LOOM_")
            self.assertEqual(provider.protocol, "responses")
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    async def test_streaming_provider_emits_deltas_and_final_response(self):
        provider = OpenAICompatibleProvider("key", base_url="http://127.0.0.1/v1", allow_insecure_http=False)
        def fake_stream(payload, emit=None):
            if emit is not None:
                emit(("data", {"choices": [{"delta": {"content": "hi"}}]}))
                emit(("done", {}))
            return []
        provider._stream_sync = fake_stream
        events = [item async for item in provider.stream([], [])]
        self.assertEqual(events[0].text, "hi")
        self.assertIsNotNone(events[-1].response)

    async def test_streaming_provider_joins_fragmented_tool_arguments(self):
        provider = OpenAICompatibleProvider("key", base_url="http://127.0.0.1/v1")
        def fake_stream(payload, emit=None):
            emit(("data", {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c", "function": {"name": "session_context", "arguments": '{"a"'}}]}}]}))
            emit(("data", {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": ":1}"}}]}}]}))
            emit(("done", {}))
            return []
        provider._stream_sync = fake_stream
        events = [item async for item in provider.stream([], [])]
        self.assertEqual(events[-1].response.tool_calls[0].arguments, {"a": 1})

    async def test_broker_registers_local_artifact_batch(self):
        registry = Registry()
        store = InMemoryStore()
        store.create_session("artifact")
        with tempfile.TemporaryDirectory() as directory:
            scratch = Path(directory) / "artifact" / "scratch"
            scratch.mkdir(parents=True)
            (scratch / "a.txt").write_text("a", encoding="utf-8")
            (scratch / "b.txt").write_text("b", encoding="utf-8")
            broker = ToolBroker("artifact", registry, store=store, artifact_store=LocalArtifactStore(directory))
            await broker.dispatch_dynamic_tool("publish_plan", {"plan": {"goal": "artifacts", "revision": 1, "steps": [{"id": "build", "title": "build", "kind": "dynamic"}]}})
            response = await broker.dispatch_dynamic_tool("register_artifacts", {"step_id": "build", "artifacts": [{"path": "a.txt"}, {"path": "b.txt"}]})
            self.assertTrue(response["ok"])
            self.assertEqual(response["result"]["artifact_count"], 2)

    def test_host_catalog_entries_are_searchable(self):
        registry = Registry()
        registry.register_catalog_entry("tools", {"id": "tool-x", "name": "Tool X"})
        self.assertEqual(registry.search("tool x", "tools")[0]["id"], "tool-x")
        registry.register_capability(id="hidden", name="hidden", metadata={"path": "/secret", "description": "safe"})
        self.assertNotIn("path", registry.catalog()["capabilities"][-1])

    async def test_json_rpc_bridge_exposes_tools_and_dispatches(self):
        registry = Registry()
        store = InMemoryStore()
        store.create_session("rpc")
        bridge = AppServerBridge(ToolBroker("rpc", registry, store=store))
        listed = await bridge.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        self.assertTrue(listed["result"]["tools"])
        context = await bridge.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "session_context", "arguments": {}}})
        self.assertFalse(context["result"]["isError"])
        unknown = await bridge.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "missing", "arguments": {}}})
        self.assertTrue(unknown["result"]["isError"])

    async def test_capability_parameters_are_validated_and_passed(self):
        seen = {}
        registry = Registry()

        async def work(context):
            seen.update(context.parameters)
            return StepResult(summary="ok")

        registry.register_capability(
            id="typed",
            name="typed",
            handler=work,
            input_schema={"type": "object", "required": ["value"], "properties": {"value": {"type": "integer"}}, "additionalProperties": False},
            parameter_schema={"type": "object", "required": ["mode"], "properties": {"mode": {"enum": ["fast"]}}, "additionalProperties": False},
        )
        store = InMemoryStore()
        store.create_session("typed-s")
        broker = ToolBroker("typed-s", registry, store=store)
        await broker.dispatch_dynamic_tool("publish_plan", {"plan": {"goal": "typed", "revision": 1, "steps": [{"id": "x", "title": "x", "kind": "capability", "capability": "typed"}]}})
        invalid = await broker.dispatch_dynamic_tool("run_capability", {"capability_id": "typed", "step_id": "x", "inputs": {"value": "bad"}, "parameters": {"mode": "fast"}})
        self.assertFalse(invalid["ok"])
        valid = await broker.dispatch_dynamic_tool("run_capability", {"capability_id": "typed", "step_id": "x", "inputs": {"value": 2}, "parameters": {"mode": "fast"}})
        self.assertTrue(valid["ok"])
        self.assertEqual(seen, {"mode": "fast"})

    async def test_failed_capability_uses_semantic_error_code(self):
        registry = Registry()
        store = InMemoryStore()
        store.create_session("failure-code")
        async def fail(context):
            raise RuntimeError("no")
        registry.register_capability(id="fail", name="fail", handler=fail)
        broker = ToolBroker("failure-code", registry, store=store)
        await broker.dispatch_dynamic_tool("publish_plan", {"plan": {"goal": "x", "revision": 1, "steps": [{"id": "f", "title": "f", "kind": "capability", "capability": "fail"}]}})
        response = await broker.dispatch_dynamic_tool("run_capability", {"capability_id": "fail", "step_id": "f", "inputs": {}})
        self.assertEqual(response["error_code"], "BROKER_CAPABILITY_EXECUTION_FAILED")


if __name__ == "__main__":
    unittest.main()
