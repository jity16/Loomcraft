import { useEffect, useState } from "react";
import {
  consumeSse,
  LoomcraftWorkbench,
  initialState,
  reduceEvent,
  type LoomcraftEvent,
  type LoomcraftState,
} from "@loomcraft/renderer";
import "@loomcraft/renderer/styles.css";

/** Minimal host-owned event integration; replace the URL with your API. */
export default function App() {
  const [state, setState] = useState<LoomcraftState>(initialState);
  useEffect(() => {
    const controller = new AbortController();
    void fetch("/api/v1/loomcraft/sessions/demo/events?after_seq=0", { headers: { Accept: "text/event-stream" }, signal: controller.signal })
      .then((response) => consumeSse(response, { signal: controller.signal, onEvent: (event: LoomcraftEvent) => setState((previous) => reduceEvent(previous, event)) }))
      .catch((error: unknown) => { if ((error as { name?: string }).name !== "AbortError") console.error(error); });
    return () => controller.abort();
  }, []);
  return <LoomcraftWorkbench state={state} />;
}
