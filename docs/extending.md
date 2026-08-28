# 扩展指南

## 1. 注册 capability

业务层可以注册一个接收兼容上下文（或 canonical NodeContext）的函数：

~~~python
from loomcraft import Registry, StepResult

registry = Registry()

async def profile(context):
    source = context.inputs["source_ref"]
    # 业务代码在这里访问自己的存储/队列；引擎不猜路径。
    profile = await domain_profile(source)
    return StepResult(
        output=profile,
        summary="profile completed",
        artifacts=[{"filename": "profile.json", "content_type": "application/json"}],
    )

registry.register_capability(
    id="table.profile",
    name="Profile a table",
    description="Compute bounded schema and quality metrics.",
    version="2.1.0",
    handler=profile,
    input_schema={
        "type": "object",
        "properties": {"source_ref": {"type": "string"}},
        "required": ["source_ref"],
        "additionalProperties": False,
    },
    parameter_schema={
        "type": "object",
        "properties": {"sample_rows": {"type": "integer", "minimum": 1}},
        "additionalProperties": False,
    },
    outputs=[{"name": "profile", "artifact_type": "json"}],
)
~~~

Handler 可以是同步函数或 async 函数。推荐：

- 在 handler 自己做输入 schema、路径白名单、字节/行数上限；
- 把长任务提交到宿主队列，回调完成后再交付结果；
- 返回 JSON 可序列化 output，并在 artifacts 中只放元数据；
- 使用 context.attempt 区分重试，保证外部写入幂等；
- 不在异常信息中放密钥、完整数据或内部路径。

## 2. 注册 workflow 与通用 kind handler

~~~python
registry.register_workflow(
    id="report.build",
    name="Build report",
    handler=build_report,
    outputs=[{"name": "html", "artifact_type": "text/html"}],
)

registry.register_handler("dynamic", run_dynamic_analysis)
registry.register_handler("review", verify_result)
registry.register_handler("answer", write_narrative)
~~~

workflow 是 Plan 中的一个受控节点；内部可以包含宿主自己的子 DAG，但不要把子 DAG
的私有节点 ID暴露给模型，除非你愿意把它们作为稳定 API 维护。

非执行型领域工具可以通过 ToolBroker 的 extra_tool_handlers 注入，键名与
PlannerAgent 的 extra_tools 规范对应；核心工具名会优先保留，不能被覆盖。

## 3. 处理人工审批

在需要人确认的 handler 中抛出 ApprovalRequired：

~~~python
from loomcraft import ApprovalRequired

async def verify_result(context):
    if confidence(context.inputs) < 0.8:
        raise ApprovalRequired(
            "confidence is below the publication threshold",
            {"confidence": 0.73, "review_url": "/reviews/123"},
        )
    return {"summary": "automatically verified"}
~~~

运行会写入 waiting_approval 状态和 payload。宿主 UI 收集决定后：

1. 调用 broker.approve_run(run_id, step_id, approved=..., comment=...)；
2. 通过时该复核边界记为 succeeded，调度器继续下游；拒绝时记为 failed；
3. 记录 approver、时间和评论到宿主审计系统。

ApprovalRequired 适合“人工判断本身就是这一步结果”的 review。若审批通过后才允许执行
不可逆副作用，应把副作用做成 requires_approval=True 的 Capability；Engine 会在调用
runner 之前暂停，通过后才以 context.config["approved"] = True 执行一次。

## 4. 替换 SessionStore

默认 `SessionStore(root)` 是安全的文件会话实现；如果宿主已有数据库，可以实现
`SessionStoreProtocol` 并注入给自定义 HTTP/队列适配器。轻量兼容 API 中的
`InMemoryStore` / `JsonStore` 仍作为兼容测试实现导出。

实现 SessionStore 所需的最小方法：

~~~python
class MyStore:
    def get_current_plan(self, session_id): ...
    def publish_plan(self, session_id, plan): ...
    def update_current_plan(self, session_id, plan): ...
    def append_event(self, session_id, event, data): ...
    def read_events(self, session_id, after=0): ...
    def record_execution(self, session_id, execution): ...
    def list_executions(self, session_id): ...
~~~

如果要使用默认 Broker，还应提供 create_session/ensure_session、list_plan_history、
pending_input_requests、list_uploads、list_artifacts 和 register_artifact。数据库实现
应在 publish plan + append event + 状态更新之间使用事务或 outbox，保证前端不会看到
“节点完成但没有 execution_finished”的半状态。

## 5. 接入知识库和目录

ToolBroker 接受两个可选适配器：

- catalog_provider(query, scope, limit)：返回 JSON 行，适用于 operations、tools、skills、
  runners 等业务目录；
- knowledge_provider.list/search/read(payload)：返回逻辑路径和有界文本，不应返回宿主
  绝对路径。

路径型知识库应自己做 POSIX 相对路径校验、版本 pin、大小上限和 MIME 白名单。把版本
放进 session context，使同一轮研究可重放。

## 6. 自定义 AI Provider

只实现 AIProvider：

~~~python
class MyProvider:
    async def complete(self, messages, tools, *, model=None, temperature=None):
        raw = await my_sdk.generate(messages=messages, tools=tools)
        return AIResponse(
            text=raw.text,
            tool_calls=[
                ToolCall(call.id, call.name, call.arguments)
                for call in raw.tool_calls
            ],
            usage=raw.usage,
        )
~~~

不要在 Provider 里执行工具；PlannerAgent 会把工具交给 Broker。若模型 API 支持流式
文本，可在宿主层把 delta 转成 message_delta 事件，最终仍用同一 ToolCall 结构。

## 7. 自定义 HTTP / WebSocket

FastAPI 只是可选示例：

~~~python
from fastapi import FastAPI
from loomcraft import LoomcraftRuntime, create_fastapi_router

runtime = LoomcraftRuntime(registry, store=my_store, provider=my_provider)
app = FastAPI()
app.include_router(create_fastapi_router(runtime, prefix="/api"))
~~~

其他框架直接复用：

- POST /sessions → runtime.create_session
- POST /sessions/{id}/tools/{name} → broker.dispatch_dynamic_tool
- GET /sessions/{id}/events → EventLog.subscribe + encode_sse
- 对 WebSocket：把 event.as_dict() JSON 序列化后发送即可

认证、CSRF、CORS、租户授权、Origin 白名单和速率限制必须在宿主实现。

## 8. 自定义 artifact 存储

StepResult.artifacts 只是元数据。默认 InMemoryStore/JsonStore 会给每个 artifact 分配
id 并保存元数据；真正的文件复制、对象存储、病毒扫描和下载 URL 应由宿主实现
register_artifact。建议字段：

~~~json
{
  "id": "artifact-123",
  "filename": "result.csv",
  "display_name": "Quality result",
  "size": 18420,
  "checksum": "sha256...",
  "content_type": "text/csv",
  "step_id": "profile",
  "download_url": "/files/artifact-123"
}
~~~

## 9. 扩展 renderer

不需要 fork 组件即可：

- 覆盖 --lc-* CSS variables；
- 传入 labels 替换状态/种类文本；
- 使用 layoutPlan 自己绘制节点；
- 使用 reduceEvent 的结果喂给 Zustand/Redux；
- 自定义 EventTimeline，保留 id/seq 去重策略；
- 在 PlanStep.metadata 中放 UI 标签（图标、组、链接），renderer 会安全忽略未知字段。

若要完全替换视觉，保留 Plan/Step 的公开字段和状态语义即可，未来 renderer 版本仍可
消费同一事件流。

## 10. 版本和迁移策略

- capability/workflow ID 一旦被已发布计划引用，就当作 API 维护；
- handler 行为变化时递增 spec.version，并在 registry metadata 中声明兼容窗口；
- Plan schema 破坏性变化时新增 schema 版本，不要重解释旧字段；
- 事件名称只新增不重命名，未知事件必须可忽略；
- 迁移前先回放历史事件，确认 old revision 的状态和 artifacts 可读取。

需要独立的本地上传适配器时可使用 LocalUploadStore；它提供大小/会话配额、SHA-256、
私有目录和删除回滚。默认 FastAPI adapter 直接使用 SessionStore 的上传区；自定义
HTTP adapter 可接入 LocalUploadStore，对象存储场景则实现同样的
save_stream/path/list_uploads 接口。

执行前可调用 LocalUploadStore.verify 或 LocalArtifactStore.verify 重新计算 checksum，
把内容漂移转成可审计的失败，而不是继续使用被替换的文件。

SourceResolver 进一步把 upload:id / artifact:id 转为已验证的 host path；handler 应优先
接收这个 resolver 的结果，不要接受模型提供的绝对路径。
