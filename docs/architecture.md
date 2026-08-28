# 架构说明

## 1. 边界与依赖方向

Loomcraft 的依赖方向只有一个：

~~~text
宿主应用
  ├─ 注册 capability / workflow handler
  ├─ 提供 SessionStore、知识库和表格检查器
  ├─ 负责认证、租户隔离、队列和资源配额
  └─ 暴露 HTTP/WebSocket/SSE

Loomcraft
  ├─ Plan 模型与 DAG 验证
  ├─ ToolBroker（模型信任边界）
  ├─ Engine / PlanExecutor（调度、重试、审批、取消）
  ├─ EventLog（状态事实）
  └─ AI Provider（协议适配）

前端
  └─ @loomcraft/renderer：事件归约 + SVG 画布 + 时间线
~~~

核心包不导入宿主的数据库、文件目录、业务注册表或专业工具。所有业务调用都从
Registry 进入；所有持久化都经过 SessionStore。因此可以把同一套核心用于科研分析、
数据管道、内容生产或内部运维。

## 2. 一次完整生命周期

1. 宿主创建 session，并把上传资料/上下文挂到 session。
2. Agent 调用 session_context、catalog_search 或宿主扩展工具。
3. Agent 调用 publish_plan。Broker 用严格模型校验图结构、注册表授权和 revision
   规则，成功后写入 plan_published。
4. Agent 可以逐步调用 run_capability / run_workflow，也可以调用 execute_plan
   交给调度器自动执行。
5. Engine 为每个就绪节点建立 NodeContext，独立分支并行，持续发出
   execution_progress（兼容 DAGExecutor 还会发出 step_attempt、step_retry）。
6. runner 返回 NodeResult 后，Engine 规范化产物、更新步骤状态并发出
   artifact_registered / execution_finished。
7. runner 可以返回 needs_approval，或由 requires_approval 触发前置审批；运行进入
   paused_approval。宿主 UI 收集决定后调用 approve_run/Run.approve 继续。
8. 前端通过 SSE/WebSocket/自有消息总线归约事件。断线时使用最后的 seq 重放。

## 3. 状态机

### Run

~~~text
created ──► running ──► succeeded
                 ├──────► failed
                 ├──────► cancelled
                 └──────► paused_approval ──► running
~~~

### Step

~~~text
pending ─► ready ─► running ─► succeeded
                         ├────► failed ───► running (retry)
                         ├────► waiting_approval
                         ├────► skipped
                         └────► cancelled
~~~

plan.update_step 是计划状态转换入口。一个已经成功的节点不能被静默改成失败；
需要重新计算时，Agent 发布更高 revision 的计划。Broker 也禁止模型直接更新
capability/workflow 节点，避免绕过执行审计。

## 4. 调度器如何保证并行与依赖

Engine 先做一次拓扑校验，然后反复执行以下循环：

- 找出所有依赖均为 succeeded 的 pending 节点；
- 按 max_concurrency 创建异步 task；
- 等待任一 task 完成，写回结果和状态；
- 对失败节点根据 on_failure 决定停止、继续或等待审批；
- 继续释放下一层节点，直到没有 pending/running 节点。

当多个节点互不依赖时，它们共享同一轮调度，因此可并发执行。节点 runner 可以是
普通函数或 async def。普通同步 runner 在当前事件循环中执行；会阻塞的工作应由宿主显式放入
asyncio.to_thread、队列或独立 worker。

### 失败策略

| on_failure | 行为 |
| --- | --- |
| stop（默认） | 当前运行标记失败，未启动的下游跳过。 |
| continue | 当前节点失败仍记录；允许直接下游继续，但最终 run 状态仍是 failed，避免把部分失败伪装成成功。 |
| require_approval | 失败后转为审批边界；runner 也可返回 needs_approval。 |

### 重试

retry.max_attempts 表示总尝试次数（包含第一次）。每次失败会写入
step_retry，可使用 backoff_seconds、backoff_multiplier 和 max_backoff_seconds
做指数退避。达到上限后才把节点置为 failed。超时由 timeout_seconds 统一包裹，
超时同样进入重试路径。

execute/execute_plan 还可以设置 run 级 timeout_seconds；到期会发出 run_timeout，取消
仍在运行的节点并将 run 标记为 cancelled。宿主若需要“超时但后台任务仍在清理”的更细
生命周期，可在 handler/队列适配器中扩展事件字段。

## 5. 事件是事实源

事件 envelope：

~~~json
{
  "schema": "loomcraft-event-v1",
  "seq": 17,
  "event": "execution_progress",
  "data": {
    "run_id": "run-abc",
    "nodes": {"extract": "succeeded", "report": "running"},
    "_event_seq": 17
  },
  "ts": "2026-08-27T05:00:00Z"
}
~~~

- seq 在一个 session 内单调递增；
- Event.as_dict() 会附带 data._event_seq 兼容字段；标准 to_dict() 只包含公开 envelope；
- EventLog.read(after_seq=N) 读取历史，subscribe(callback) 交付实时事件；
- SessionStore 使用目录内 JSON/JSONL 文件，适合单进程；多进程场景实现同样的接口并使用宿主
  的事务/锁；
- UI 不应依赖轮询猜测状态，也不应把本地乐观状态当成审计事实。

建议宿主保留以下事件索引：plan_published、step_updated、execution_started、
execution_progress、step_retry、artifact_registered、execution_finished、
tool_call、tool_result、error。

## 6. AI 层与执行层的隔离

PlannerAgent 只是一个 tool-loop：

~~~text
messages + tool specs
        ▼
     AIProvider
        ▼ AIResponse(tool_calls)
     ToolBroker
        ▼
 validation / registry / executor
        ▼
  tool result + event
        └──────────────► 下一轮模型上下文
~~~

Provider 不拥有执行权限；Broker 不解析模型的内部思考；Executor 不知道模型是哪家。
这使得同一个 DAG 可以由 OpenAI Responses、Chat Completions、Codex CLI 或人工 API
调用触发。

## 7. 部署建议

- 单体应用：在 Web 进程内创建一个 Runtime，使用 JsonStore 或宿主数据库。
- 队列执行：handler 只提交 job，并在 job 完成时返回/更新 StepResult；不要在请求
  线程中运行长时间 shell。
- 多租户：每个请求先在宿主层鉴权，再把正确的 session_id 传给 Broker；不要把
  session_id 当作授权凭证。
- 生产 AI：使用 Secret Manager 注入 API key；Provider 的异常只返回受限摘要。
- 取消/重试：宿主需要持久化 run id，并在 worker 重启后从事件日志恢复或显式标记
  interrupted，不能把进程退出当作成功。

## 8. 可替换点

| 接口 | 默认实现 | 可替换用途 |
| --- | --- | --- |
| Registry | 内存注册表 | 业务能力、远端 job、版本化工具目录 |
| SessionStore | InMemoryStore / JsonStore | PostgreSQL、Redis、对象存储 |
| AIProvider | OpenAI-compatible / Scripted | 本地模型、企业网关、Codex |
| table_inspector | 无（可选） | pandas、DuckDB、宿主安全服务 |
| knowledge_provider | 无（可选） | 版本化 Markdown、向量检索、文献库 |
| HTTP adapter | 可选 FastAPI | Starlette、Flask、GraphQL、WebSocket |
| renderer skin | styles.css | CSS variables、设计系统或无障碍高对比主题 |
