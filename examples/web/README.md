# Web integration sketch

Copy `App.tsx` into a React/Vite host, install `@loomcraft/renderer`, and point
the fetch URL at the server adapter. The server emits named SSE events, so the
example uses `consumeSse` rather than relying on EventSource's default message
handler. Applications that already parse
SSE can call `reduceEvent` directly and pass the resulting state to
`LoomcraftWorkbench`.

`Demo.tsx` seeds a revisioned plan with parallel, running, and retry-attempt
states so the renderer can be previewed before connecting a backend.
