# @loomcraft/renderer

React components and state for LoomCraft plans. The only peer dependency is
React — the DAG layout, the pan/zoom canvas, and the SSE reader are first-party.

```bash
npm install @loomcraft/renderer
```

```tsx
import { LoomWorkbench } from "@loomcraft/renderer";
import "@loomcraft/renderer/styles.css";

<LoomWorkbench sessionId={sessionId} baseUrl="/api/v1/loomcraft" />
```

Three layers — take as many as you need:

```ts
// 1 · Pure state, no React
import { initialLoomState, reduceLoomEvent, hydrateLoomState } from "@loomcraft/renderer";

// 2 · Transport
import { LoomClient } from "@loomcraft/renderer";

// 3 · UI
import { useLoomSession, PlanGraph, Timeline, ArtifactList } from "@loomcraft/renderer";
```

`reduceLoomEvent` has the exact `(state, action) => state` shape `useReducer`
wants, and `hydrateLoomState` replays a persisted history through the same
function — so live updates and a mid-run refresh cannot disagree.

Theme by overriding `--lc-*` custom properties. Dark mode follows
`prefers-color-scheme` or `data-lc-theme="dark"`.

Full documentation:
<https://github.com/jity16/Loomcraft/blob/main/docs/04-frontend-integration.md>

## Tests

```bash
npm test        # node --test --experimental-strip-types
npm run build
```
