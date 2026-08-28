# API 速查

## Python

### Models

- Plan.from_raw(raw) / Plan.to_dict()
- Plan.validate(registry=None)、Plan.to_json()/from_json()
- RUN_STATUSES / STEP_STATUSES：可用于宿主状态机和 UI 过滤
- PlanStep、RetryPolicy、AnalysisObjective、AnalysisCoverage
- validate_plan(raw, current=None, registry=None)
- update_step(current, step_id, status, summary=None, execution=None)
- topological_order(plan)、topological_layers(plan)
- validate_input_request、allocate_input_uploads、validate_input_fulfillment
- task_phase(plan, busy=False)
- diff_plans(previous, current)

### Registry

- Registry.register_capability(CapabilitySpec(...)) 或关键字参数 id/name/handler
- Registry.register_workflow(WorkflowSpec(...))
- Registry.register_handler(kind, handler)
- Registry.capability(id)、workflow(id)、catalog()、search(query, scope, limit)
- StepResult(output, summary, artifacts, metadata, status, error)

### Executor

~~~python
executor = DAGExecutor(
    registry,
    store=store,
    max_concurrency=4,
    on_event=lambda event: print(event),
)
result = await executor.execute(plan, session_id="s", inputs={})
await executor.cancel(result.run_id)
~~~

- StepContext：session_id、run_id、plan、step、inputs、dependencies、attempt、
  cancel_event、emit
- ApprovalRequired(message, payload)
- ExecutionResult.as_dict()/to_json()
- executor.execute_step(...)：给 Broker 的单节点受权执行
- executor.execute_dag(...)：将 nodes/edges DAG 转换后执行
- executor.run_plan(...)：execute 的语义别名
- executor.cancel(run_id)、wait(run_id)、active_runs(session_id)
- executor.known_runs(session_id)（包含等待审批的 run）
- executor.approve(...)：处理需要审批的 run（也可由宿主按自己的审计流程恢复）

### Broker

~~~python
broker = ToolBroker(session_id, registry, store=store, executor=executor)
response = await broker.dispatch_dynamic_tool("publish_plan", {"plan": raw_plan})
~~~

返回：

~~~json
{"ok": true, "result": {"plan": {}}}
~~~

失败时：

~~~json
{"ok": false, "error": "plan or step validation failed: ...", "error_code": "BROKER_PLAN_INVALID"}
~~~

dynamic_tool_specs() 返回所有工具的 OpenAI/Codex 双格式规范。

### Storage / Events

- InMemoryStore：测试/嵌入式运行
- JsonStore(root)：单进程 JSON + JSONL 持久化
- EventLog.append/read/subscribe(callback)；subscribe 返回 unsubscribe
- Event.as_dict()、encode_sse(event)、iter_sse(log, after)
- LocalUploadStore：带大小限制和 SHA-256 的可选本地上传存储
- LocalArtifactStore：从 session scratch 安全复制/登记产物
- SourceResolver：解析并校验 session-owned upload/artifact/scratch 引用
- inspect_table_file：有界 CSV/TSV/JSON/SQLite 只读画像
- validate_dag / plan_from_dag：兼容 nodes/edges 工作流格式

### AI

- OpenAICompatibleProvider(api_key, base_url, model, protocol, extra_params)
- ResponsesAPIProvider(api_key, base_url, model)
- JsonlSubprocessProvider(command, cwd, env, timeout)
- CodexCLIProvider(codex_bin, cwd, sandbox)
- ScriptedProvider(responses)
- PlannerAgent(provider, broker, max_rounds, system_prompt)
- StreamingAIProvider / AIStreamEvent（可选流式文字与工具片段）
- parse_chat_response、parse_responses_response、openai_tool_specs

### Server

LoomcraftRuntime 组合 registry/store/provider；run_turn 提供串行的 Agent turn；AppServerBridge 是 JSON-RPC transport
桥；create_fastapi_router(runtime) 是可选
FastAPI 适配器。没有安装 FastAPI 时，导入核心仍然不受影响。

适配器提供 `/tools`、`/sessions`、`/sessions/{id}/context`、`/history`、`/events`、
`/turn`、`/tools/{name}`、`/runs/{run}/cancel`、`/runs/{run}/approve`、
`/executions/{run}/approve`、uploads、artifacts 和输入 fulfillment 路由；
认证、Origin、租户隔离和限流由宿主负责。
Runtime.delete_session 只删除指定 session；生产宿主应在调用前做权限和保留策略检查。

## TypeScript

从 @loomcraft/renderer 导出：

| 导出 | 用途 |
| --- | --- |
| PlanGraph | 可平移/缩放的 Plan DAG 与 revision 切换 |
| LoomWorkbench | 自带 HTTP/SSE、消息、输入、审批和产物的完整工作台 |
| LoomcraftWorkbench | 宿主传入 reducer state 的兼容工作台 |
| EventTimeline | 活动时间线 |
| layoutPlan / assignLayers / fitToViewport | 确定性布局、分层与视口适配 |
| parsePlan | 对服务端 Plan payload 做安全归一化 |
| reduceEvent / hydrateState | 事件归约、历史恢复 |
| consumeSse | fetch Response 的 SSE 解析器 |
| LoomcraftClient | 最小 HTTP/SSE 客户端 |
| Plan、PlanStep、Execution、LoomcraftEvent | 跨端类型 |

## 事件名称

| 事件 | 最小 data |
| --- | --- |
| plan_published | plan |
| step_updated | revision、step |
| execution_started | run_id、execution_kind |
| step_attempt | run_id、step_id、attempt |
| step_retry | run_id、step_id、next_attempt、delay_seconds |
| execution_progress | execution_id + node_id/status，或兼容的 run_id + nodes |
| artifact_registered | step_id、artifact |
| execution_finished | execution（含 revision、steps、failed_nodes、artifacts） |
| tool_call | item_id、tool |
| tool_result | item_id、ok |
| input_required | request |
| message/message_delta | text 或 delta |
| error | message |
| done | status |

## 兼容性承诺（0.x）

- JSON 字段新增优先，旧字段不重解释；
- 未知事件和 metadata 可忽略；
- Broker 错误码保持稳定；
- 0.x 仍可能在主版本内调整实验性 Server 路由，核心模型/事件会提前在 changelog
  说明。
