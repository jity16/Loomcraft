import { strict as assert } from "node:assert";
import test from "node:test";
import { consumeSse } from "../src/transport.ts";
import {
  createIntelligentClientToken,
  withIntelligentRequestTimeout,
} from "../src/client.ts";

test("consumeSse parses named frames and ids", async () => {
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode("id: 4\nevent: plan_published\ndata: {\"event\":\"plan_published\",\"data\":{\"ok\":true}}\n\n"));
      controller.close();
    },
  });
  const events: unknown[] = [];
  await consumeSse(new Response(body), { onEvent: (event) => events.push(event) });
  assert.equal(events.length, 1);
  assert.equal((events[0] as { seq?: number }).seq, 4);
  assert.equal((events[0] as { event: string }).event, "plan_published");
});

test("consumeSse flushes a final frame without a blank line", async () => {
  const body = new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode("event: done\ndata: {\"event\":\"done\",\"data\":{}}"));
      controller.close();
    },
  });
  const events: unknown[] = [];
  await consumeSse(new Response(body), { onEvent: (event) => events.push(event) });
  assert.equal(events.length, 1);
});

test("compatibility client helpers expose a token and timeout error", async () => {
  assert.match(createIntelligentClientToken(), /^[0-9a-f-]{16,}$/i);
  await assert.rejects(
    withIntelligentRequestTimeout(5, "timed out", async (signal) => {
      await new Promise<void>((resolve, reject) => {
        const timer = setTimeout(resolve, 50);
        signal.addEventListener("abort", () => {
          clearTimeout(timer);
          reject(new Error("aborted"));
        });
      });
      return true;
    }),
    (error: unknown) =>
      error instanceof Error && error.name === "IntelligentRequestTimeoutError",
  );
});
