<div align="center">

# LoomCraft

**智能体提出探索路径，运行时保证它真的发生，界面实时呈现证据。**

一个可嵌入、厂商中立的运行时，专门处理会随着证据变化的工作：科学发现、证据综述、
长耗时数据分析，以及任何“下一步取决于上一步发现了什么”的流程。

[English](README.md) · **简体中文**

[![Python 3.11+](https://img.shields.io/badge/python-3.11+-38bdf8?style=flat-square&logo=python&logoColor=white&labelColor=0b1120)](packages/core/pyproject.toml)
[![React 18+](https://img.shields.io/badge/react-18+-a78bfa?style=flat-square&logo=react&logoColor=white&labelColor=0b1120)](packages/renderer/package.json)
[![核心依赖：仅 pydantic](https://img.shields.io/badge/%E6%A0%B8%E5%BF%83%E4%BE%9D%E8%B5%96-%E4%BB%85%20pydantic-fbbf24?style=flat-square&labelColor=0b1120)](packages/core/pyproject.toml)
[![测试：311 通过](https://img.shields.io/badge/%E6%B5%8B%E8%AF%95-311%20%E9%80%9A%E8%BF%87-34d399?style=flat-square&labelColor=0b1120)](#测试)
[![许可证：MIT](https://img.shields.io/badge/%E8%AE%B8%E5%8F%AF%E8%AF%81-MIT-f472b6?style=flat-square&labelColor=0b1120)](LICENSE)

[为什么是 LoomCraft](#为长周期探索而生) · [接入后得到什么](#你接入什么-loomcraft-增强什么) · [架构](#架构) · [快速开始](#快速开始) · [文档](docs/) · [示例](examples/)

<br>

<img src="assets/workbench-tour.zh.svg" width="980"
     alt="LoomCraft 工作台：用户需求和智能体发布的计划位于左侧，执行图位于右侧。变异规范化后扇出到 PCA、协变量准备和亲缘矩阵，证据汇聚一次，再扇出三条 GCTA 分析线；每条线都有质量核验，最后经过服务端 review 和报告。">

<sub>一张会演进的计划、三层并行、一个可审计的结果。图片使用的卡片几何和状态 token
与 <code>@loomcraft/renderer</code> 完全相同。</sub>

</div>

---

## 为长周期探索而生

短工具调用并不难编排，真正困难的是开放式工作：从一个问题开始，逐步收集证据，发现
原来的假设不成立，第二天还能接着做，而且今天发生过的事不能丢。

LoomCraft 把这种过程当成一等公民。Plan 是有版本的结构化数据，而不是藏在聊天记录里的
提示词；产物和事件可以跨 turn 保留；互不依赖的调查会同时运行；有副作用的步骤可以在
启动前等待人工批准；如果问题无法回答，记录会说明原因和下一步怎样才能回答。

| 场景 | 工作中会发生什么 | LoomCraft 提供什么 |
| --- | --- | --- |
| **科学发现** | 诊断结果改变方法，或暴露出缺失的分析 | 带理由的版本、objective/证据覆盖、产物溯源和核验步骤 |
| **文献/证据综述** | 缺文件、研究不兼容或空分支改变结论 | 类型化输入请求、依赖感知的跳过、重试和诚实的失败记录 |
| **长耗时数据分析** | 工具运行数小时，需要重试、取消、恢复和进度 | 整图执行、有界策略、持久会话、SSE 重放和 artifacts |
| **人工参与的操作** | 结果只有在人员确认后才变成外部动作 | runner 调用前的审批闸口，以及可审计的决策事件 |
| **Agent 化工程/运维** | 模型根据诊断结果选择下一步，而不是盲跑固定 runbook | 只暴露宿主能力的窄工具面，以及统一的图和状态保证 |

共同点不在于领域，而在于时间和不确定性：计划可以改变，执行历史仍然可信。

## 你接入什么，LoomCraft 增强什么

LoomCraft 是一条边界，不是一套业务应用。你带来已经信任的模型运行时、业务函数、存储
和传输层；库负责给它们加上契约和守卫。

| 你带来 | LoomCraft 增加 | 实际效果 |
| --- | --- | --- |
| 任意模型运行时 | 一份规范工具目录和一个 Broker 入口 | Claude、OpenAI 兼容端点、本地 JSONL 进程和 Codex 可以互换，执行语义不变 |
| 业务函数和 workflow | 类型化输入、参数、输出端口和 Registry 授权 | Agent 可以组合你的能力，但不能调用任意代码或虚构 capability |
| 依赖关系图 | DAG 校验、确定性分层、有界并发、重试、超时和取消 | 提高吞吐并让恢复可预测，不需要 `parallel=True` 开关 |
| 文件和中间结果 | 会话级 source ref、校验和、产物提升和版本历史 | 长任务可恢复、可审计，宿主路径不会泄露给模型或浏览器 |
| HTTP 或 app-server 宿主 | 可选 FastAPI/SSE 和 JSON-RPC 适配器 | 实时进度、断线重连、审批，以及统一的线上授权路径 |
| React 应用（或什么都没有） | 纯 reducer、确定性布局、SVG 图和完整工作台 | 在 React、其他 UI 框架或自有 Canvas 中显示同一份事实 |

### 各类运行时都走同一条执行路径

| 运行时 | 入口 |
| --- | --- |
| Anthropic Messages | `AnthropicAgent()` |
| OpenAI 兼容 Chat Completions | `OpenAICompatibleAgent(...)` |
| 另一个进程（JSONL） | `SubprocessAgent([...])` |
| Codex / app-server | `AppServerBridge(broker)` |
| 自定义模型循环 | 实现 `Agent.run_turn(...)` 协议 |

## 架构

模型负责提案，宿主拥有能力目录，Broker 是唯一入口，Engine 是唯一能让服务端步骤变成
真实状态的组件，Renderer 只是事件日志的投影。因此刷新页面和实时流最终会收敛到同一份状态。

```text
用户问题 / 文件
       │
       ▼
 Agent / 模型运行时 ── publish_plan ──► 版本化 Plan
       │                                  │
       │ 工具调用                         ▼
       └────────────────────────────► ToolBroker
                                         │ 校验 + 授权
                    宿主 Registry ───────┤
                    （你的 runner）       ▼
                                        Engine
                                         │ 并行 / 重试 / 闸口
                                         ▼
                                  EventLog + artifacts
                                         │
                                  SSE / history
                                         ▼
                                      Renderer
```

<div align="center">
<img src="assets/architecture.zh.svg" width="900"
     alt="LoomCraft 架构：智能体调用 Broker，Broker 授权 Engine，事件日志驱动 Renderer，业务 runner 由宿主注册。">
</div>

Python 核心包在 [`packages/core/src/loomcraft/`](packages/core/src/loomcraft/)，React 包在
[`packages/renderer/`](packages/renderer/)。两者共享事件契约，但彼此不绑定实现语言。

其他契约图仍然保留在仓库中。为避免首页变得拥挤，下面默认折叠：

<details>
<summary>展开：重新规划、步骤权限、生命周期和会话信任区</summary>

<p align="center">
<img src="assets/plan-execution.zh.svg" width="760" alt="核验发现结果被混杂后，计划发布新版本重新规划。">
</p>

<p align="center">
<img src="assets/step-kinds.zh.svg" width="760" alt="步骤 kind 决定由智能体还是服务端写入状态。">
</p>

<p align="center">
<img src="assets/step-lifecycle.zh.svg" width="760" alt="步骤状态转移表。">
</p>

<p align="center">
<img src="assets/session-zones.zh.svg" width="760" alt="LoomCraft 会话的四个信任区。">
</p>
</details>

## 读懂执行图

开场工作台不是线性的 hello-world，而是一张扇出、汇聚、再扇出的探索图：

```text
normalize ─┬─ pca ───────────┐
           ├─ phenotype ─────┼─ assemble ─┬─ scan.yield  ── qc.yield  ─┐
           └─ kinship ───────┘             ├─ scan.depth  ── qc.depth  ──┼─ review ── report
                                          └─ scan.height ── qc.height ─┘
```

`pca`、`phenotype` 和 `kinship` 互不依赖，因此会在同一轮并发；`assemble` 只把共享证据
组装一次；三个分析和三个核验又各自形成并行层。并行来自 `depends_on` 的图形，不是模型
需要记住的特殊关键字。

## 快速开始

安装引擎并注册宿主允许的能力：

```bash
python -m pip install -e packages/core
```

```python
from loomcraft import Capability, NodeContext, NodeResult, Port, Registry

registry = Registry()

@registry.capability_runner(Capability(
    id="table.profile",
    name="Profile a table",
    description="Count rows and report the column names.",
    runner="table.profile",
    outputs=(Port(name="profile", artifact_type="json"),),
))
async def profile(ctx: NodeContext) -> NodeResult:
    ctx.emit("profile", "profile.json", '{"columns": 12, "rows": 480}')
    return NodeResult.ok(summary="profile complete")
```

把会话和 Broker 交给 Agent，或者从自己的模型循环调用相同的工具：

```python
from loomcraft import SessionStore, ToolBroker

session = SessionStore("./.loomcraft-data").create()
broker = ToolBroker(session, registry)
broker.begin_turn()

await broker.dispatch("publish_plan", {"plan": {
    "goal": "Profile the uploaded table",
    "revision": 1,
    "steps": [{
        "id": "profile",
        "title": "Profile the table",
        "kind": "capability",
        "capability": "table.profile",
    }],
}})
run = await broker.dispatch("execute_plan", {})
assert run.ok and run.result["status"] == "succeeded"
```

真实模型可以换成 `AnthropicAgent`、`OpenAICompatibleAgent`、`SubprocessAgent`，或自行实现
`Agent.run_turn(...)`。

### 接入 Renderer

```bash
cd packages/renderer
npm ci
npm run build
npm install /path/to/Loomcraft/packages/renderer
```

```tsx
import { LoomWorkbench } from "@loomcraft/renderer";
import "@loomcraft/renderer/styles.css";

<LoomWorkbench sessionId={sessionId} baseUrl="/api/v1/loomcraft" />
```

也可以只使用 `reduceLoomEvent`、`hydrateLoomState`、`LoomClient` 或 `PlanGraph`。

## 运行示例

命令行输出放在这里，先让图和架构说明产品，再看运行证据。首先运行
[Workbench Tour](examples/00-workbench-tour/)：

```bash
python examples/00-workbench-tour/run.py
```

它会在执行前拒绝循环图，测量两层并发，重试一次瞬时失败，在审批闸口暂停，并验证事件哈希链：

```text
validation        cycle refused before execution=True
revision 1 · 13 steps
layer 0  normalize
layer 1  kinship + pca + phenotype       ← 同一轮调度
layer 2  assemble
layer 3  scan.yield + scan.depth + scan.height  ← 同一轮调度
layer 4  qc.yield + qc.depth + qc.height       ← 同一轮调度
layer 5  review
layer 6  report                           ← 等待审批

parallel window  pca, phenotype, kinship  overlap=0.16s
parallel window  scan.yield, scan.depth, scan.height  overlap=0.10s
retry            scan.depth               attempt 2/2
approval         report                   runner calls=0
run              succeeded                13/13 个节点有记录
report runner    invoked after approval       calls=1
```

更深入的场景：

- [Association study](examples/01-gwas-discovery/)：科学重新规划、产物核验、输入 variants、SSE 和浏览器工作台。
- [Literature meta-analysis](examples/02-literature-meta/)：输入请求、证据分支、失败/跳过传播和 Claude 路径。
- [Objectives and scheduling](examples/03-objectives-and-scheduling/)：证据台账、容错失败、服务端 review 和 JSON-RPC。
- [示例能力覆盖矩阵](examples/README.md)。

## 引擎保证

- **运行前失败关闭。** 环、重复 id、未知依赖、超大计划和未授权能力在 Broker 边界被拒绝。
- **服务端工作不能伪造。** `capability`、`workflow` 只能由执行工具完成；review 可以绑定服务端能力。
- **证据跨 turn 保留。** artifacts、objective 覆盖、版本和哈希链事件可以跨重试、重连和长时间运行保存。
- **路径不会越界。** `upload:`、`artifact:`、`scratch:` 引用始终限于会话，并在读取时重新校验。
- **恢复过程可见。** 重试、超时、取消、失败策略和审批决定都会出现在图和事件历史中。

## 文档

从 [`docs/README.md`](docs/README.md) 开始：

- [概念](docs/01-concepts.md)：计划、步骤、能力、会话、事件
- [定义计划](docs/02-defining-plans.md)：schema、校验、策略、objectives
- [Agent 集成](docs/03-agent-integration.md)：工具、循环、Provider、守卫
- [前端集成](docs/04-frontend-integration.md)：reducer、SSE、组件、主题
- [扩展](docs/05-extending.md)：runner、workflow、存储、传输层
- [架构](docs/06-architecture.md)：设计决策和取舍
- [API 参考](docs/07-api-reference.md)：公开 Python、TypeScript、事件和端点

机器可读的 Plan、Event、Tool 契约在 [`packages/core/schema/`](packages/core/schema/)。

## 测试

```bash
python -m pip install -e "packages/core[dev]"
python -m pytest -q                         # Python 257 个测试
python -m ruff check packages/core/src --select F,E9,B023
python tools/check_docs.py

npm --prefix packages/renderer ci
npm --prefix packages/renderer run typecheck
npm --prefix packages/renderer run build
npm --prefix packages/renderer test             # Renderer 54 个测试
```

## 许可证

MIT
