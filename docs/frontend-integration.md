# 前端集成

## 1. 安装与包边界

renderer 是独立的 React + TypeScript 包，不依赖 React Flow、Tailwind 或宿主路由：

~~~bash
# 尚未发布到 npm，先构建一次，再从本地路径安装
git clone https://github.com/jity16/Loomcraft.git
cd Loomcraft/packages/renderer && npm ci && npm run build
cd /path/to/your-app
npm install /path/to/Loomcraft/packages/renderer react react-dom
~~~

首次加载样式：

~~~tsx
import "@loomcraft/renderer/styles.css";
~~~

组件默认使用纸张感的浅色界面，并自动响应 prefers-color-scheme；蓝色表示运行中，
绿/红色表示完成/失败，审批使用警示色。所有颜色、圆角和字体都以
--lc-* CSS variables 暴露，可以在宿主主题中覆盖。

## 2. 事件归约

如果宿主自管网络层，可读取 SSE、WebSocket 或轮询事件后调用 reduceEvent：

~~~tsx
import {
  initialState,
  reduceEvent,
  LoomcraftWorkbench,
  type LoomcraftEvent,
} from "@loomcraft/renderer";

const [state, setState] = useState(initialState);

function onEvent(event: LoomcraftEvent) {
  setState((previous) => reduceEvent(previous, event));
}

return <LoomcraftWorkbench state={state} />;
~~~

reduceEvent 的设计原则：

- 每个 plan revision 按 revision 排序，重复 plan_published 会更新而不是重复卡片；
- step_updated 只替换对应 id，保留其他步骤；
- execution_started/progress/finished 按 kind + id 合并；
- artifact_registered 去重；
- message/tool/input 事件进入时间线；
- 未知事件安全忽略，便于服务端向前兼容。

服务端重启或页面刷新时可用 hydrateState(history) 从 plans、events、executions、
artifacts 和 uploads 重建状态。

## 3. SSE

### 使用原生 fetch

~~~tsx
const response = await fetch("/api/v1/loomcraft/sessions/s-1/events?after_seq=42", {
  headers: { Accept: "text/event-stream" },
  signal,
});
await consumeSse(response, {
  onEvent: (event) => setState((old) => reduceEvent(old, event)),
});
~~~

consumeSse 解析 event、id、data 行，支持多行 data 和中断信号。标准事件 envelope
带有 seq；兼容 iter_sse 也会输出 SSE id。重连时把最后处理的 seq 作为 after_seq。
服务端会定期发送注释 heartbeat，代理不会因长时间无节点变化而关闭连接。

### 使用 LoomcraftClient

~~~tsx
const client = new LoomcraftClient({ baseUrl: "/api/v1/loomcraft" });
await client.streamEvents(
  "s-1",
  (event) => setState((old) => reduceEvent(old, event)),
  {
    afterSeq: lastSeq,
    signal,
  },
);
~~~

推荐在宿主保存 lastSeq；断线后从已确认处理的序号重连。不要只依赖内存里的节点颜色，
因为 durable event log 才是恢复依据。

reducer 会把已处理的 seq 保存在 state.lastSeq，并忽略重复或倒序事件；宿主可以把它
作为下次 after cursor 的起点。

Client 还提供 session/history、turn、upload、input request、approval、artifact 和 cancel
方法；需要直接调用 publish_plan/execute_plan 时，可请求服务端的 tools/{tool_name}
路由。宿主仍应在所有路由前完成认证和权限判断。

## 4. 组件选择

### PlanGraph

只显示一张 plan：

~~~tsx
<PlanGraph
  plan={state.currentPlan!}
  selectedStepId={selectedId}
  onSelectStep={setSelectedId}
  plans={state.plans}
/>
~~~

它输出可平移/缩放的 DAG、依赖箭头、状态颜色和 revision 切换。节点本身是可聚焦
button，accessible label 包含 kind、状态和依赖。layoutPlan 可以单独调用，得到 nodes/edges/width/height，适合自定义
Canvas、导出 PNG 或测试布局。

### LoomcraftWorkbench

组合 PlanGraph、Timeline 和 ArtifactList：

~~~tsx
<LoomcraftWorkbench
  state={state}
  showTimeline
  onStepSelect={(step) => openDrawer(step)}
/>
~~~

这是由宿主提供 state 的兼容组件；需要自带 HTTP/SSE、输入请求、审批和消息 composer
的完整页面时，使用 LoomWorkbench，并传入 sessionId 与 baseUrl。

### EventTimeline

如果宿主已有自己的 DAG 画布，只使用：

~~~tsx
<EventTimeline items={state.timeline} />
~~~

## 5. 无障碍与响应式

- 节点是原生 button，可用 Tab、Enter、Space 选择，accessible label 包含依赖；
- 状态文字和颜色同时出现，色觉差异不会阻断判断；
- 移动端时间线自动移动到画布下方；
- prefers-reduced-motion 会关闭执行边流动、脉冲和轨道旋转；
- 宿主若需要高对比度，可覆盖 --lc-ink、--lc-hairline、--lc-run 等变量，并保持
  3:1/4.5:1 的对比度检查。

## 6. 自定义主题

~~~css
.my-research-theme {
  --lc-canvas: #0b1220;
  --lc-surface: #111c30;
  --lc-graph-canvas: #0f1728;
  --lc-accent: #79c995;
  --lc-run: #79b8ff;
  --lc-ok: #a8c66c;
  --lc-err: #ff9696;
  --lc-ink: #f4f8ff;
}
~~~

组件不读取 Tailwind class，也不覆盖宿主全局 button 样式；可以把变量挂在更窄的容器
上实现页面级主题。

## 7. 后端事件约定

renderer 依赖的最小事件：

~~~json
{"event":"plan_published","data":{"plan":{}}}
{"event":"step_updated","data":{"revision":1,"step":{"id":"x","status":"running"}}}
{"event":"execution_started","data":{"execution_id":"r","execution_kind":"plan","status":"running"}}
{"event":"execution_progress","data":{"execution_id":"r","nodes":{"x":"running"}}}
{"event":"artifact_registered","data":{"artifact":{"id":"a","filename":"out.csv"}}}
{"event":"execution_finished","data":{"execution":{"id":"r","status":"succeeded","artifacts":[]}}}
~~~

message、tool_call、tool_result、input_required 是可选但推荐的体验增强事件。宿主可以
增加自定义事件；归约器会忽略不认识的名称，宿主也可在自己的 reducer 中消费它们。

## 8. SSR 与非 React 场景

layoutPlan、assignLayers、fitToViewport、reduceEvent 都不访问 window。SSR 时可以先
hydrateState，再把 PlanGraph 放入客户端交互边界。若不使用 React，直接读取
layoutPlan 的节点矩形和边路径，自行渲染 SVG/Canvas 即可；这也是把同一计划嵌入
报告或监控屏的推荐方式。

状态和节点类型文案目前由组件提供英文默认值；需要本地化时可基于 layoutPlan 与
reduceEvent 组合自有节点组件，或在宿主封装层替换可见文本。
