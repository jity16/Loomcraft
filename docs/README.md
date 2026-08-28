# Loomcraft 文档

下面列出的内容都是面向使用者和集成者的公开文档；实现过程中的工作记录不属于发布文档。

- [架构说明](architecture.md)：层次、生命周期、状态机、调度与部署。
- [Plan 定义](plan-definition.md)：字段、kind、DAG 校验、重试、输入请求。
- [Agent 工具](agent-tools.md)：publish_plan、ToolBroker、Provider 和 tool-loop。
- [前端集成](frontend-integration.md)：React renderer、SSE、主题和无障碍。
- [扩展指南](extending.md)：注册业务能力、存储、知识库、审批和自定义 Provider。
- [API 速查](api-reference.md)：Python、TypeScript、事件名称。

深入英文参考：

- [Concepts](01-concepts.md)
- [Defining plans](02-defining-plans.md)
- [Agent integration](03-agent-integration.md)
- [Frontend integration](04-frontend-integration.md)
- [Extending LoomCraft](05-extending.md)
- [Architecture decisions](06-architecture.md)
- [API reference](07-api-reference.md)

机器可读的契约位于 [Plan](../packages/core/schema/plan.schema.json)、[Event](../packages/core/schema/event.schema.json)
和 [Tools](../packages/core/schema/tools.schema.json)。可执行示例位于 [examples/python](../examples/python/)。
