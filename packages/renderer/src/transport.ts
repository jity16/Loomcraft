import type { LoomEvent } from "./types";
import { LoomClient } from "./client.js";

export interface SseOptions {
  signal?: AbortSignal;
  onEvent: (event: LoomEvent) => void;
}

/** Consume the shared JSON SSE envelope; comments/heartbeats are ignored. */
export async function consumeSse(response: Response, options: SseOptions): Promise<void> {
  if (!response.ok) throw new Error(`LoomCraft event stream failed (${response.status})`);
  if (!response.body) throw new Error("LoomCraft event stream has no body");
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let name = "message";
  let id: number | undefined;
  let data: string[] = [];
  const flush = () => {
    if (!data.length) return;
    const raw = data.join("\n");
    try {
      const parsed = JSON.parse(raw) as Record<string, unknown>;
      options.onEvent({
        seq: typeof parsed.seq === "number" ? parsed.seq : id,
        event: typeof parsed.event === "string" ? parsed.event : name,
        data: parsed.data ?? parsed,
        ts: typeof parsed.ts === "string" ? parsed.ts : undefined,
      });
    } catch {
      // A malformed frame is recoverable: the caller can reconnect from seq.
    }
    data = [];
    name = "message";
    id = undefined;
  };
  try {
    for (;;) {
      const chunk = await reader.read();
      buffer += decoder.decode(chunk.value ?? new Uint8Array(), { stream: !chunk.done });
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        if (!line) {
          flush();
        } else if (line.startsWith(":")) {
          continue;
        } else if (line.startsWith("event:")) {
          name = line.slice(6).trim() || "message";
        } else if (line.startsWith("id:")) {
          const value = Number(line.slice(3).trim());
          if (Number.isFinite(value)) id = value;
        } else if (line.startsWith("data:")) {
          data.push(line.slice(5).replace(/^ /, ""));
        }
      }
      if (chunk.done) {
        if (buffer) {
          if (buffer.startsWith("data:")) data.push(buffer.slice(5).replace(/^ /, ""));
          else if (buffer.startsWith("event:")) name = buffer.slice(6).trim() || "message";
        }
        flush();
        break;
      }
      if (options.signal?.aborted) {
        await reader.cancel();
        throw new DOMException("The operation was aborted", "AbortError");
      }
    }
  } finally {
    reader.releaseLock();
  }
}

/** Compatibility class name; canonical transport remains ``LoomClient``. */
export class LoomcraftClient extends LoomClient {}
