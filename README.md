# Loomcraft

[English](README.en.md) · 简体中文

Loomcraft 是一套可嵌入、可替换、可观察的 AI-native DAG 执行引擎。它把
“模型理解目标并发布计划”和“受信任的运行时执行计划”分成两个边界：Agent
只能通过结构化工具发布/修订 DAG，宿主应用负责注册自己的 capability、workflow
和存储实现；前端则消费同一套事件流，实时展示计划、依赖、重试、产物和失败原因。

它从一个面向生物育种的应用中抽离而来，但库本身不包含育种字段、数据库模型、
专业二进制或业务路由。换句话说，Loomcraft 负责编排与观测，业务代码负责“做什么”。

> 状态：`0.1.0`（可用于原型和内部生产验证；接口已按可开源库的稳定边界组织）。

## 为什么是 Loomcraft

传统工作流通常在代码里预先写死一条路径；Loomcraft 允许模型根据当前上下文发布
一个可校验、可修订的执行计划，同时把执行权留在宿主注册表和运行时手中：

```text
自然语言目标 / 文件 / 上下文
          │
          ▼
  AI Provider + 原生工具协议
          │  publish_plan
          ▼
  严格校验的版本化 DAG ───────┐
          │                   │  plan_published / step_updated
          ▼                   │  execution_progress / artifacts
  依赖调度 · 并行 · 重试       ├──────────► SSE / WebSocket / 日志
          │                   │
          ▼                   │
  capability / workflow handler┘
```

核心特性：

| 能力 | 说明 |
| --- | --- |
| 可信计划 | 严格字段校验、唯一 ID、未知依赖检测、环检测、注册表授权、修订原因和目标覆盖台账 |
| AI 调用 | OpenAI-compatible Chat Completions / Responses、JSONL 子进程、可测试的 Scripted Provider |
| 原生工具 | `session_context`、`catalog_search`、`publish_plan`、`run_capability`、`run_workflow`、`execute_plan`、输入请求、产物登记、知识检索钩子 |
| 执行 | 异步 DAG 调度、独立分支并行、指数退避重试、超时、失败策略、取消、人工审批暂停/恢复 |
| 观测 | 有序、可校验的 append-only 事件（含 hash chain）、JSONL 持久化、SSE 编码与历史重放 |
| 前端 | 无 React Flow 绑定的 SVG DAG、布局优化、缩放拖拽、修订切换、节点检查器、键盘焦点和 reduced-motion 支持 |
| 数据检查 | 可选的标准库 CSV/TSV/JSON/SQLite 有界只读 inspector，也可替换为 pandas/DuckDB 适配器 |
| 解耦 | 业务 capability/workflow、持久化、知识库、表格检查、认证和 HTTP 框架全部通过注入接口接入 |

## 项目结构

```text
Loomcraft/
├── packages/core/            # 可发布的 Python 核心包（canonical）
│   ├── src/loomcraft/        # Plan、Registry、Engine、Broker、AI 与 Session
│   ├── schema/               # Plan/Event/Tool JSON Schema
│   └── tests/                # 核心契约与安全测试
├── packages/renderer/        # @loomcraft/renderer（React + TypeScript）
│   ├── src/layout.ts         # 确定性 DAG 布局与 viewport fitting
│   ├── src/state.ts          # 事件归约与历史 hydration
│   ├── src/components/       # Graph、Workbench、Panels
│   └── tests/                # 渲染、协议与兼容测试
├── core/loomcraft/           # 仅用于旧源码路径的兼容 shim
├── examples/                # 可直接运行的 Python/Web 示例
├── docs/                    # 架构、契约、扩展与集成文档
├── tests/                   # Python 单元/集成测试
└── packages/core/schema/     # 机器可读 JSON Schema
```

## 快速开始（Python）

Python 核心依赖 `pydantic`，支持 Python 3.11+：

```bash
python -m venv .venv
. .venv/bin/activate
pip install -e packages/core
```

从仓库根目录执行 `pip install -e .` 也可以安装同一个 canonical 包。
安装后可用 `python -m loomcraft --tools` 查看原生工具目录。

下面的最小程序注册一个业务 capability，发布计划并执行它：

```python
import asyncio
from loomcraft import InMemoryStore, Registry, StepResult, ToolBroker

async def main():
    registry = Registry()

    async def double(context):
        value = context.inputs.get("value", 0)
        return StepResult(output={"value": value * 2}, summary="doubled")

    registry.register_capability(
        id="math.double",
        name="Double a number",
        description="A host-owned operation.",
        handler=double,
    )

    store = InMemoryStore()
    store.create_session("demo")
    broker = ToolBroker("demo", registry, store=store)

    published = await broker.dispatch_dynamic_tool("publish_plan", {"plan": {
        "goal": "Double the input",
        "revision": 1,
        "steps": [{
            "id": "double",
            "title": "Double value",
            "kind": "capability",
            "capability": "math.double",
        }],
    }})
    assert published["ok"]

    run = await broker.dispatch_dynamic_tool(
        "execute_plan", {"inputs": {"value": 21}}
    )
    print(run["result"]["status"])  # succeeded

asyncio.run(main())
```

上面的 `InMemoryStore`/`session_id` 写法是轻量兼容入口；新项目推荐使用
`SessionStore("./data").create()`、强类型 `Capability`/`Workflow` 和
`ToolBroker(session, registry)`，两者最终经过同一个 `Engine` 与事件日志。

完整示例（包括并行分支、重试、产物和事件打印）：

```bash
python examples/python/retry_parallel.py
python examples/python/ai_planning.py       # 不需要 API key，使用 ScriptedProvider
python examples/python/approval_pause.py
```

## 接入 AI

AI 层只认识三个对象：`AIProvider`、`AIResponse` 和 `ToolCall`。模型输出的工具调用
会经过 `PlannerAgent` → `ToolBroker` → 验证/执行；模型不能直接导入业务模块或调用
宿主的任意函数。

```python
import os
from loomcraft import OpenAICompatibleProvider, PlannerAgent

provider = OpenAICompatibleProvider(
    api_key=os.environ["OPENAI_API_KEY"],
    base_url="https://api.openai.com/v1",
    model="gpt-4.1-mini",
    protocol="chat",              # 或 "responses"
)
agent = PlannerAgent(provider, broker, max_rounds=16)
answer = await agent.run("分析我上传的资料，先发布可执行计划")
```

如果宿主已经有 Codex/本地 Agent 进程，可使用 `JsonlSubprocessProvider`，让进程通过
stdin 接收 `{messages, tools}`，并以 JSONL 输出 `tool_call`、`message` 行。需要完全
控制 HTTP 客户端时，实现同样的 `AIProvider.complete(...)` 即可。

工具规范同时提供 Codex app-server 的 `inputSchema` 和 OpenAI 的 `function.parameters`，
因此同一份 `dynamic_tool_specs()` 可以直接用于两种协议。

## 接入前端

```bash
# 尚未发布到 npm：先在仓库中构建，再从本地路径安装
cd packages/renderer && npm ci && npm run build
cd /path/to/your-app && npm install /path/to/Loomcraft/packages/renderer react react-dom
```

```tsx
import React from "react";
import {
  LoomcraftWorkbench,
  initialState,
  reduceEvent,
  type LoomcraftEvent,
} from "@loomcraft/renderer";
import "@loomcraft/renderer/styles.css";

function TaskView({ events }: { events: LoomcraftEvent[] }) {
  const [state, setState] = React.useState(initialState);
  React.useEffect(() => {
    for (const event of events) setState((current) => reduceEvent(current, event));
  }, [events]);
  return <LoomcraftWorkbench state={state} />;
}
```

实时接入可以使用内置 `consumeSse(response, { onEvent })`，也可以使用
`LoomClient.streamEvents` / 兼容别名 `LoomcraftClient`。事件带有单调递增 `seq`，客户端断线后用最后一个序号
重连即可重放。画布是纯 SVG，不要求 React Flow；`layoutPlan` 和 `reduceEvent` 也可
在 SSR、Canvas 包装器或自有状态管理中单独使用。

## 核心概念

### Plan 与 revision

Plan 是 Agent 对目标的一个版本化假设。第一次发布 `revision: 1`；任何替换都必须
使用更大的 revision 并填写 `reason`。已经声明的 `objectives` 不允许在修订中被静默
删除。执行状态由服务器重置/更新，模型提供的 `status`、`summary` 和 `execution`
不会绕过可信状态机。

### Step 与依赖

Step 的 `kind` 决定授权路径：

- `capability`：引用注册表中的一个原子能力，由 `run_capability` 或调度器执行。
- `workflow`：引用注册表中的一个组合流程，由 `run_workflow` 或调度器执行。
- `dynamic` / `review`：宿主注册的通用处理器，适合动态分析或复核；review 也可绑定
  明确标记为复核用途的 capability，并由 `run_capability` 执行。
- `answer`：纯回答节点；未注册处理器时有一个安全的默认完成处理器。

`depends_on` 是唯一的依赖来源。所有上游成功后，互不相依的节点会并行运行；节点的
`retry`、`timeout_seconds` 和 `on_failure` 控制局部失败行为。

### 事件与证据

每个状态改变都生成事件，而不是让前端猜测状态。典型顺序是：

```text
plan_published
  → execution_started
  → step_updated(running)
  → step_attempt / step_retry
  → execution_progress
  → artifact_registered
  → step_updated(succeeded|failed)
  → execution_finished
```

事件存储可以是内存、JSONL 或宿主数据库。前端只需归约事件，不必知道业务 runner 的
内部实现。

## 安全边界

- 模型只能调用白名单工具；动作次数和相同动作重复次数均有上限。
- capability/workflow ID 必须来自宿主注册表，未知 ID 在发布前拒绝。
- 计划和输入请求采用拒绝未知字段策略，错误摘要不会回显整份模型 payload。
- 执行器不接受任意 shell 字符串；外部工具应由宿主 capability handler 做参数化适配。
- `JsonStore` 适合单进程；多租户/多进程场景应实现 `SessionStore` 并在宿主层完成认证、
  授权、限流、CORS、密钥管理和隔离。
- API key 只属于 Provider，事件和前端状态不会记录请求头。
- `Session` 默认将上传、artifact、scratch 和 control 分区；持久化 EventLog 通过 hash chain 检测篡改。

## 非目标与演进

Loomcraft 不试图替代任务队列、容器沙箱、对象存储或领域计算框架；它提供这些系统
之间稳定的计划/事件边界。后续 0.x 版本会优先保持 JSON/事件向后兼容，并继续补充
更多宿主适配器（数据库 outbox、WebSocket 和队列 runner），而不会把某个业务领域
写回核心包。

## 文档导航

仓库公开的文档仅包含使用、集成和扩展指南；开发过程记录不会进入发布树。

| 主题 | 文档 |
| --- | --- |
| 架构、生命周期、并行调度 | [docs/architecture.md](docs/architecture.md) |
| Plan 字段、DAG 校验、重试与覆盖台账 | [docs/plan-definition.md](docs/plan-definition.md) |
| Agent 工具、AI Provider、输入请求 | [docs/agent-tools.md](docs/agent-tools.md) |
| React/SSE 集成、主题和可访问性 | [docs/frontend-integration.md](docs/frontend-integration.md) |
| 注册 capability、持久化、知识库和自定义 runner | [docs/extending.md](docs/extending.md) |
| Python/TypeScript API 速查 | [docs/api-reference.md](docs/api-reference.md) |
| 核心概念与架构决策 | [docs/01-concepts.md](docs/01-concepts.md)、[docs/06-architecture.md](docs/06-architecture.md) |
| 完整 API 与前端参考 | [docs/07-api-reference.md](docs/07-api-reference.md)、[docs/04-frontend-integration.md](docs/04-frontend-integration.md) |

## 验证

```bash
python -m pytest -q
npm --prefix packages/renderer install
npm --prefix packages/renderer run build
npm --prefix packages/renderer test
```

也可以直接运行 `make install` 安装两侧开发依赖，或运行 `make check` 执行全部检查。

## 许可证

MIT，见 [LICENSE](LICENSE)。
