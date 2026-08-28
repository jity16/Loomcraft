# Plan 定义与校验

## 最小 Plan

一个可执行计划至少有 goal、revision 和一个 step：

~~~json
{
  "goal": "生成一份数据质量摘要",
  "revision": 1,
  "steps": [
    {
      "id": "profile",
      "title": "检查数据结构",
      "kind": "dynamic",
      "depends_on": []
    }
  ]
}
~~~

发布前可以使用 packages/core/schema/plan.schema.json 做 JSON Schema 预检；最终仍必须经过
Python 的 validate_plan，因为环检测、注册表授权和 revision 比较属于跨字段规则。

## 顶层字段

| 字段 | 类型 | 必填 | 语义 |
| --- | --- | --- | --- |
| goal | string | 是 | 用户目标，1–2000 字符。 |
| summary | string | 否 | 给 UI 的短说明。 |
| revision | integer | 是 | 从 1 开始单调递增。 |
| reason | string/null | 否 | revision > 1 时必填，说明为什么重规划。 |
| analysis_profile | string/null | 否 | 研究型任务的分析策略标签。 |
| objectives | array | 否 | 要回答的科学/业务问题清单。 |
| analysis_coverage | array | 否 | 与 objectives 一一对应的覆盖台账。 |
| steps | array | 是 | 1–256 个 DAG 节点。 |
| metadata | object | 否 | 宿主自定义、不会参与执行授权的只读元数据。 |

## Step 字段

~~~json
{
  "id": "fit-model",
  "title": "拟合模型",
  "kind": "capability",
  "capability": "stats.fit_mixed_model",
  "depends_on": ["profile"],
  "description": "根据画像选择固定效应和随机效应",
  "retry": {
    "max_attempts": 3,
    "backoff_seconds": 1,
    "backoff_multiplier": 2,
    "max_backoff_seconds": 20
  },
  "timeout_seconds": 900,
  "on_failure": "continue",
  "metadata": {"owner": "analysis"}
}
~~~

- id 必须匹配正则 ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$，且在一个 Plan 内唯一。
- depends_on 只能引用同一 Plan 的 id；不能自依赖或形成环。
- capability/workflow 节点必须有 capability，并且必须在对应 Registry 中注册。
- answer/dynamic 不能声明 capability。review 通常由 kind handler 处理，也可以绑定
  一个 runner 以 `review.` 开头、带 `review` tag，或在 metadata.step_kinds 中声明
  `review` 的已注册 capability；绑定后只能通过 run_capability 完成。
- status、summary、execution 是运行时字段。Agent 发布时即使传入它们，也会被重置为
  pending/null；运行时只能由 Broker/Executor 写入。
- retry.max_attempts 是“总尝试次数”，不是额外重试次数；范围 1–20。为兼容旧的
  nodes/edges DAG，输入 0 会归一化为 1（不重试）。
- timeout_seconds 是单次尝试的上限；超时会进入 retry 流程。
- on_failure 默认为 stop。continue 会让直接下游继续，但整体 run 仍标记 failed。
- metadata 适合放展示标签，不应放密钥、原始文件内容或未验证的命令行。

## 五种 kind

### answer

给用户的解释或总结节点。可不注册 handler，Executor 会使用安全的默认完成处理器；
需要生成报告时建议注册自己的 answer handler，返回带引用的 StepResult。

### capability

最小、类型明确、可审计的能力。例如 table.profile、image.embed 或
genetics.gwas。只有 Registry 注册的 ID 才能被发布和执行。

### workflow

宿主预先定义的组合流程。它仍然是 Plan 中的一个节点，内部细节由 workflow handler
负责，避免 Agent 直接拼接一串不受控的子命令。

### dynamic

根据当前上下文临时组织的步骤。常用于数据画像、代码生成、分析解释；宿主应为它
注册 handler，并把实际脚本/沙箱执行放在 handler 内。

### review

核验或人工复核边界。handler 可以抛出 ApprovalRequired，让运行停在
waiting_approval；审批后再恢复下游。

没有注册 review handler 时，Executor 会使用安全的默认审批边界（直接暂停而不执行
副作用），适合从 legacy human.approval 节点转换的 DAG。

## DAG 规则

Loomcraft 采用依赖列表而不是隐式数组顺序：

~~~text
extract ──┬──► profile ──► report
          └──► validate ──┘
~~~

extract 完成后 profile 和 validate 可并行；report 要等两者成功。使用
topological_order(plan) 可以得到稳定的执行顺序，使用 topological_layers(plan) 可以
得到用于 UI/调度的层级。

失败依赖的处理：

1. 上游 succeeded：下游正常就绪。
2. 上游 failed/cancelled/skipped 且上游 on_failure=stop：下游 skipped。
3. 上游 failed 且上游 on_failure=continue：下游仍可运行，但整体执行结果为 failed。
4. 上游 waiting_approval：整个 run 保持 waiting_approval。

如果宿主已有 nodes/edges 格式，可用 validate_dag 校验后用 plan_from_dag 转成
AI-native Plan；转换会把原 node type 放进 step.metadata.source_node_type，不会丢掉
版本/来源信息。外部工具和报告节点只有在 config.capability 存在时才会映射为受权的
capability/workflow，其余节点需要宿主注册 dynamic/review handler。

## 研究目标与覆盖台账

当 Plan 声明 objectives 时，必须同时声明 analysis_coverage，并且每个 objective 只能
有一条 coverage：

~~~json
{
  "objectives": [
    {
      "id": "reliability",
      "question": "数据是否足以支持可靠性估计？",
      "estimand": "repeatability",
      "expected_outputs": ["估计表", "不确定性说明"]
    }
  ],
  "analysis_coverage": [
    {
      "objective_id": "reliability",
      "status": "planned",
      "reason": "先完成数据画像再选择估计方法",
      "selected_method": "pending method discovery",
      "step_ids": ["profile"]
    }
  ]
}
~~~

状态含义：

- planned：已纳入计划但尚未有执行证据；
- executed：已有 step_ids 或 artifact_refs 作为证据；
- not_estimable：资料/设计不支持估计，必须写 next_action；
- blocked：被外部依赖阻断，必须写 next_action；
- deferred_by_scope：明确留到后续范围，必须写 next_action。

修订时，旧 Plan 中的 objective id 不能被悄悄删除；如果研究范围确实变化，应在新的
目标里保留说明并由宿主做显式迁移/归档。

## 输入请求

Agent 缺资料时调用 request_inputs，而不是发布一个“假完成”的计划：

~~~json
{
  "title": "需要补充输入",
  "message": "缺少配套的索引文件。",
  "requirements": [
    {
      "key": "index",
      "label": "索引文件",
      "description": "与主表同前缀的索引",
      "required": true,
      "min_files": 1,
      "max_files": 1,
      "allowed_extensions": [".tbi", ".idx"],
      "field_hints": ["文件名应与主表前缀一致"]
    }
  ],
  "continue_prompt": "资料已补齐，请重新读取上传清单并继续。"
}
~~~

服务器生成 request_id；相同 checksum 的上传只分配一次。allocate_input_uploads 会先
满足 required/min_files，再在 max_files 范围内分配可选文件。宿主收到资料后应记录
input_fulfilled 事件，并开始新一轮 Agent turn。

## 版本化与兼容

Plan JSON 是跨语言边界，建议：

- 持久化原始发布版本和每次运行快照；
- 新增字段先进入 metadata 或升级 schema 版本；
- 不要复用旧 step id 表示语义完全不同的节点；
- renderer 只依赖公开字段，未知 metadata 可安全忽略；
- 业务注册表变更后，旧 Plan 仍可读取，但再次执行前应重新授权/迁移。
