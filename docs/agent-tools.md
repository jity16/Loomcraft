# Agent 工具与 AI 接口

## 1. 原生工具表面

dynamic_tool_specs() 返回同时兼容 Codex app-server 和 OpenAI tools 的规范。核心动作分为
三类：

### 只读探索

| 工具 | 用途 |
| --- | --- |
| session_context | 获取当前计划、上传、执行、产物、待补资料和注册表快照。 |
| catalog_search | 搜索 capability/workflow，以及由宿主注入的 operations/tools/skills/runners。 |
| capability_search | 只搜索可执行的原子能力，不会启动执行。 |
| operation_search | 交给宿主语义目录检索方法和输入输出契约。 |
| inspect_table | 交给宿主的有界表格检查器，不修改源数据。 |
| knowledge_list/search/read | 交给版本固定的 knowledge_provider。 |

### 计划与输入

| 工具 | 用途 |
| --- | --- |
| publish_plan | 验证并发布完整 DAG；发布成功才允许执行。 |
| update_step | 更新 answer/dynamic 及未绑定 capability 的 review 节点。 |
| request_inputs | 生成结构化补资料请求，并把当前 Agent turn 锁定为 waiting_inputs。 |

### 执行与证据

| 工具 | 用途 |
| --- | --- |
| run_capability | 执行被计划授权的 capability 节点或绑定 capability 的 review 节点。 |
| run_workflow | 执行被计划授权的 workflow 节点。 |
| execute_plan | 通过 PlanExecutor 把整张 Plan 交给同一个 Engine 调度并行分支和重试。 |
| register_artifact(s) | 将宿主已验证的输出登记到 session，并关联步骤。 |

Broker 会把 action 名中的连字符归一化为下划线，因此 publish-plan 也能安全地映射到
publish_plan，但建议模型使用规范名称。

## 2. 推荐的 Agent 循环

~~~text
1. session_context
2. （需要时）capability_search / operation_search / inspect_table
3. publish_plan
4. execute_plan，或按依赖逐个 run_capability/run_workflow
5. 读取 execution_progress / artifact_registered
6. 失败时解释证据并发布 revision + reason
7. 资料不足时 request_inputs，等待新 turn
~~~

execute_plan 的 inputs 推荐按 step id 分组，避免多个 capability 的参数或输入键冲突：

~~~json
{
  "inputs": {
    "profile": {
      "inputs": {"table": "upload:up-123"},
      "parameters": {"sample_rows": 200}
    }
  }
}
~~~

显式 source ref 会在调度前校验会话归属、checksum、扩展名和参数范围；依赖步骤产生的
artifact 则按 output port 自动绑定到下游 input key。

不要让模型直接调用 shell、数据库或任意 HTTP。外部工具的命令行、路径检查、资源锁
和结果解析都应放在宿主注册的 handler 中。

若使用本地上传/产物适配器，先让 SourceResolver 解析 upload:id 或 artifact:id，再把
内部 path 交给 inspector/handler；绝对路径永远不应作为模型参数接受。

## 3. ToolBroker 信任边界

每个 session 创建一个 ToolBroker：

~~~python
broker = ToolBroker(
    session,
    registry,
    limits=BrokerLimits(
        max_actions_per_turn=64,
        max_identical_actions=3,
    ),
    table_inspector=inspect_table,
    catalog_provider=search_host_catalog,
    knowledge_provider=knowledge,
)
~~~

Broker 的动作流程：

1. 归一化 action 和 JSON payload；
2. 检查动作总数与重复签名；
3. 对只读动作直接查询宿主适配器；
4. 对 publish_plan/request_inputs/update_step 调用严格模型验证；
5. 对执行动作核对 step kind、capability ID、依赖状态和当前状态；
6. 返回稳定的 {ok, result, error, error_code}，不把异常栈交给模型。

一个 Broker 可以跨 turn 复用；每次新 Agent turn 应调用 begin_turn（PlannerAgent 会自动
调用），以重新开始动作预算，同时保留计划、事件和输入请求历史。

常见错误码：

| code | 含义 |
| --- | --- |
| BROKER_PLAN_INVALID | Plan、依赖或状态转换失败。 |
| BROKER_AWAITING_INPUTS | 本轮正在等待用户文件。 |
| BROKER_EXECUTION_BUSY | 已有执行占用 session。 |
| BROKER_ACTION_LIMIT_EXCEEDED | 达到本轮动作预算。 |
| BROKER_ACTION_REPEATED | 相同动作重复且没有进展。 |
| BROKER_INVALID_ARGUMENT | 参数类型、范围或对象形状不合法。 |
| BROKER_KNOWLEDGE_UNAVAILABLE | knowledge snapshot 在同一 session 中发生变化。 |
| BROKER_UNSUPPORTED_ACTION | 宿主没有配置该扩展。 |
| BROKER_INTERNAL_ERROR | 未分类的安全边界错误。 |

## 4. AIProvider

Provider 只需实现：

~~~python
async def complete(messages, tools, *, model=None, temperature=None) -> AIResponse:
    ...
~~~

AIResponse 包含 text、tool_calls、finish_reason、usage 和可选 raw。每个 ToolCall 包含
id、name、arguments（已经解析为 object）。

### OpenAI-compatible

OpenAICompatibleProvider 使用标准库 urllib，不强制安装 SDK：

~~~python
provider = OpenAICompatibleProvider(
    api_key=os.environ["OPENAI_API_KEY"],
    base_url="https://api.openai.com/v1",
    model="gpt-4.1-mini",
    protocol="chat",       # responses 也可用
)
~~~

它会解析 Chat Completions 的 choices[0].message.tool_calls，以及 Responses 的
output.function_call；tool-loop 会把历史工具消息转换为 Responses 原生的
function_call/function_call_output items。自定义网关只要保持对应响应形状即可。远程 HTTP 默认拒绝，只有
localhost 或显式 allow_insecure_http=True 才可使用。

### JSONL 子进程

JsonlSubprocessProvider 适合已有 Codex/Agent harness：

- stdin：一行 JSON，包含 messages、tools、model；
- stdout：零到多行 JSON；
- type=tool_call：id/name/arguments；
- type=message 或 response：text/content；
- 非零退出码或超时会变成 AIProviderError。

密钥通过宿主环境注入，不会写入 tool 事件。

### ScriptedProvider

用于单元测试、离线演示和契约验收。它按顺序返回预置 AIResponse，且保留 calls
列表，便于断言模型确实看到了动态工具规范。

## 5. PlannerAgent tool-loop

PlannerAgent 每轮：

1. 把 system prompt、历史和用户消息交给 Provider；
2. 如果有文字，发出 message 事件；
3. 将每个 ToolCall 写入 tool_call；
4. 调用 broker.dispatch_dynamic_tool；
5. 将受限的工具结果作为 role=tool 追加回上下文；
6. 没有工具调用时发出 done；
7. request_inputs 成功时返回 waiting_inputs；
8. 超过 max_rounds 时发出 error。

宿主可通过 PlannerAgent(extra_tools=[...]) 追加只读领域工具；追加工具仍应在宿主侧做
参数校验和权限检查。核心执行工具始终由 ToolBroker 提供，不能被同名扩展覆盖。绑定
review capability 的复核步骤同样只能通过 run_capability 完成；未绑定的 review 才由
update_step 上报。

Provider 可以被替换而不影响执行和前端。生产环境建议设置 max_rounds、请求超时和
宿主侧 token/成本预算。

将 PlannerAgent 的 stream 设为 true 时，支持 stream() 的 Provider 会把文本片段转成
message_delta 事件；最终的完整 AIResponse 仍用于解析工具调用，因而不会因为流式显示
而改变 publish_plan 的信任边界。

## 6. 资料请求与恢复

request_inputs 成功后，Broker 在该实例上设置 waiting latch。宿主收到文件并完成
校验后：

1. 记录 input_fulfilled（包含 request_id 和分配结果）；
2. 创建新的 Agent turn/新的 Broker；
3. 让模型先调用 session_context，再继续原目标；
4. 如旧计划已不适用，发布更高 revision，并填写 reason。

这样可以避免模型在资料尚未到达时反复执行同一个动作。

## 7. 宿主扩展示例

~~~python
async def inspect_table(source_ref, options):
    # 在这里做路径白名单、字节上限和格式解析
    return {"source": source_ref, "shape": {"sample_rows": 20, "columns": 5}}

async def search_catalog(query, scope, limit):
    return [{"id": "stats.fit", "scope": scope, "description": "..."}]

class Knowledge:
    async def search(self, payload):
        return {"query": payload["query"], "results": []}

broker = ToolBroker(
    "session-1",
    registry,
    table_inspector=inspect_table,
    catalog_provider=search_catalog,
    knowledge_provider=Knowledge(),
)
~~~

扩展返回值应是 JSON 可序列化对象，并自行限制字节、行数、路径和敏感字段。

如果宿主使用 Codex app-server 或 JSON-RPC，可以把同一个 Broker 交给 AppServerBridge。
它支持 initialize、tools/list 和 tools/call；传输层仍由宿主负责读写 JSONL/WebSocket、
进程生命周期和鉴权。
