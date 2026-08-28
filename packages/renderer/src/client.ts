/**
 * Browser client for the LoomCraft HTTP API.
 *
 * The SSE reader is hand-rolled rather than using `EventSource` for two reasons:
 * starting a turn is a POST with a JSON body (EventSource is GET-only), and we
 * need the frame boundary logic anyway to distinguish "the turn ended" from "my
 * connection dropped". Those are different outcomes — the first is terminal, the
 * second means the work is still running server-side and a reconnect with
 * `after_seq` will catch up.
 */

import type { Artifact, History, LoomEvent, Upload } from "./types";

export class LoomHttpError extends Error {
  readonly status: number;
  readonly code: string | null;

  constructor(message: string, status: number, code: string | null = null) {
    super(message);
    this.name = "LoomHttpError";
    this.status = status;
    this.code = code;
  }
}

export class LoomProtocolError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "LoomProtocolError";
  }
}

/** Compatibility names used by the original intelligent-mode client. */
export class IntelligentHttpError extends LoomHttpError {}
export class IntelligentProtocolError extends LoomProtocolError {}
export class IntelligentRequestTimeoutError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "IntelligentRequestTimeoutError";
  }
}

export async function withIntelligentRequestTimeout<T>(
  timeoutMs: number,
  message: string,
  action: (signal: AbortSignal) => Promise<T>,
): Promise<T> {
  if (!Number.isFinite(timeoutMs) || timeoutMs <= 0) {
    throw new RangeError("timeoutMs must be a positive finite number");
  }
  const controller = new AbortController();
  let timedOut = false;
  const timer = globalThis.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  try {
    return await action(controller.signal);
  } catch (error) {
    if (timedOut) {
      throw new IntelligentRequestTimeoutError(message, { cause: error });
    }
    throw error;
  } finally {
    globalThis.clearTimeout(timer);
  }
}

export function createIntelligentClientToken(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") {
    return globalThis.crypto.randomUUID();
  }
  const bytes = new Uint8Array(16);
  globalThis.crypto?.getRandomValues?.(bytes);
  return Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
}

/** How a turn stream ended. `detached` means the work continues server-side. */
export type TurnOutcome =
  | { state: "terminal"; ok: boolean }
  | { state: "detached" }
  | { state: "aborted" };

export interface LoomClientOptions {
  baseUrl?: string;
  /** Extra headers on every request (auth, tenant, tracing). */
  headers?: Record<string, string>;
  fetchImpl?: typeof fetch;
}

const TURN_COMPLETE = Symbol("loomcraft-turn-complete");

async function readError(prefix: string, response: Response): Promise<LoomHttpError> {
  const raw = await response.text().catch(() => "");
  let detail = raw;
  let code: string | null = null;
  try {
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const nested = parsed.detail;
    if (typeof nested === "string") detail = nested;
    else if (nested && typeof nested === "object") {
      const row = nested as Record<string, unknown>;
      if (typeof row.message === "string") detail = row.message;
      if (typeof row.code === "string") code = row.code;
    }
    if (typeof parsed.error_code === "string") code = parsed.error_code;
  } catch {
    // A plain-text gateway error is still useful as-is.
  }
  return new LoomHttpError(
    `${prefix} (${response.status})${detail ? `: ${detail}` : ""}`,
    response.status,
    code,
  );
}

export class LoomClient {
  private readonly baseUrl: string;
  private readonly headers: Record<string, string>;
  private readonly fetchImpl: typeof fetch;

  constructor(options: LoomClientOptions = {}) {
    this.baseUrl = (options.baseUrl ?? "/api/v1/loomcraft").replace(/\/$/, "");
    this.headers = { ...options.headers };
    this.fetchImpl = options.fetchImpl ?? globalThis.fetch.bind(globalThis);
  }

  private url(path: string): string {
    return `${this.baseUrl}${path}`;
  }

  private async request<T>(
    path: string,
    init: RequestInit,
    errorPrefix: string,
  ): Promise<T> {
    const response = await this.fetchImpl(this.url(path), {
      ...init,
      headers: { ...this.headers, ...(init.headers as Record<string, string> | undefined) },
    });
    if (!response.ok) throw await readError(errorPrefix, response);
    if (response.status === 204) return undefined as T;
    try {
      return (await response.json()) as T;
    } catch (error) {
      throw new LoomProtocolError(`${errorPrefix}: response was not JSON`, {
        cause: error,
      });
    }
  }

  // ── Sessions ──────────────────────────────────────────────────────────────

  createSession(signal?: AbortSignal): Promise<Record<string, unknown>> {
    return this.request("/sessions", { method: "POST", signal }, "Could not create session");
  }

  listSessions(signal?: AbortSignal): Promise<{ sessions: string[] }> {
    return this.request("/sessions", { signal }, "Could not list sessions");
  }

  getHistory(sessionId: string, afterSeq = 0, signal?: AbortSignal): Promise<History> {
    return this.request(
      `/sessions/${encodeURIComponent(sessionId)}/history?after_seq=${afterSeq}`,
      { signal },
      "Could not load task history",
    );
  }

  deleteSession(sessionId: string, signal?: AbortSignal): Promise<{ deleted: boolean }> {
    return this.request(
      `/sessions/${encodeURIComponent(sessionId)}`,
      { method: "DELETE", signal },
      "Could not delete session",
    );
  }

  getCatalog(signal?: AbortSignal): Promise<{
    capabilities: Array<Record<string, unknown>>;
    workflows: Array<Record<string, unknown>>;
  }> {
    return this.request("/catalog", { signal }, "Could not load catalog");
  }

  // ── Uploads ───────────────────────────────────────────────────────────────

  async uploadFile(
    sessionId: string,
    file: File,
    signal?: AbortSignal,
  ): Promise<Upload> {
    const body = new FormData();
    body.append("file", file);
    return this.request(
      `/sessions/${encodeURIComponent(sessionId)}/uploads`,
      { method: "POST", body, signal },
      `Could not upload ${file.name}`,
    );
  }

  deleteUpload(
    sessionId: string,
    uploadId: string,
    signal?: AbortSignal,
  ): Promise<{ deleted: string; invalidated_request_ids: string[] }> {
    return this.request(
      `/sessions/${encodeURIComponent(sessionId)}/uploads/${encodeURIComponent(uploadId)}`,
      { method: "DELETE", signal },
      "Could not delete file",
    );
  }

  // ── Input requests & approvals ────────────────────────────────────────────

  fulfillInputRequest(
    sessionId: string,
    requestId: string,
    signal?: AbortSignal,
  ): Promise<Record<string, unknown>> {
    return this.request(
      `/sessions/${encodeURIComponent(sessionId)}/input-requests/${encodeURIComponent(requestId)}/fulfill`,
      { method: "POST", signal },
      "Could not confirm the uploaded files",
    );
  }

  cancelInputRequest(
    sessionId: string,
    requestId: string,
    signal?: AbortSignal,
  ): Promise<Record<string, unknown>> {
    return this.request(
      `/sessions/${encodeURIComponent(sessionId)}/input-requests/${encodeURIComponent(requestId)}/cancel`,
      { method: "POST", signal },
      "Could not cancel the file request",
    );
  }

  approveNode(
    sessionId: string,
    runId: string,
    nodeId: string,
    approved: boolean,
    signal?: AbortSignal,
  ): Promise<Record<string, unknown>> {
    return this.request(
      `/sessions/${encodeURIComponent(sessionId)}/executions/${encodeURIComponent(runId)}/approve`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ node_id: nodeId, approved }),
        signal,
      },
      "Could not record the approval",
    );
  }

  // ── Artifacts ─────────────────────────────────────────────────────────────

  listArtifacts(sessionId: string, signal?: AbortSignal): Promise<{ artifacts: Artifact[] }> {
    return this.request(
      `/sessions/${encodeURIComponent(sessionId)}/artifacts`,
      { signal },
      "Could not list artifacts",
    );
  }

  artifactUrl(sessionId: string, artifactId: string): string {
    return this.url(
      `/sessions/${encodeURIComponent(sessionId)}/artifacts/${encodeURIComponent(artifactId)}`,
    );
  }

  async downloadArtifact(
    sessionId: string,
    artifactId: string,
    filename: string,
  ): Promise<void> {
    const response = await this.fetchImpl(this.artifactUrl(sessionId, artifactId), {
      headers: this.headers,
    });
    if (!response.ok) throw await readError(`Could not download ${filename}`, response);
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    anchor.click();
    URL.revokeObjectURL(url);
  }

  // ── Turns ─────────────────────────────────────────────────────────────────

  cancelTurn(sessionId: string, signal?: AbortSignal): Promise<{ cancelled: boolean }> {
    return this.request(
      `/sessions/${encodeURIComponent(sessionId)}/cancel`,
      { method: "POST", signal },
      "Could not stop the task",
    );
  }

  /**
   * Start a turn and stream its events.
   *
   * Resolves `terminal` when the server sent `done`, `detached` when the stream
   * ended without one (the turn is still running — reconnect with
   * {@link streamEvents}), and `aborted` when the caller cancelled.
   */
  async runTurn(
    sessionId: string,
    message: string,
    onEvent: (event: LoomEvent) => void,
    signal?: AbortSignal,
  ): Promise<TurnOutcome> {
    // A mutable holder rather than a bare `let`: TypeScript's control-flow
    // analysis cannot see assignments made inside the callback below, and would
    // otherwise narrow the variable to `null` at every read site.
    const outcome: { terminal: { ok: boolean } | null; accepted: boolean } = {
      terminal: null,
      accepted: false,
    };

    try {
      await this.readSse(
        this.url(`/sessions/${encodeURIComponent(sessionId)}/turn`),
        {
          method: "POST",
          headers: {
            ...this.headers,
            Accept: "text/event-stream",
            "Content-Type": "application/json",
          },
          body: JSON.stringify({ message }),
          signal,
        },
        (event) => {
          onEvent(event);
          if (event.event === "done") {
            const data = (event.data ?? {}) as Record<string, unknown>;
            outcome.terminal = { ok: data.ok !== false };
            throw TURN_COMPLETE;
          }
        },
        () => {
          outcome.accepted = true;
        },
      );
    } catch (error) {
      if (error === TURN_COMPLETE && outcome.terminal) {
        return { state: "terminal", ok: outcome.terminal.ok };
      }
      if (signal?.aborted) return { state: "aborted" };
      // The POST was accepted, so the turn exists server-side even though we
      // lost the stream. Do not report this as a failed task.
      if (outcome.accepted) return { state: "detached" };
      throw error;
    }
    return outcome.terminal
      ? { state: "terminal", ok: outcome.terminal.ok }
      : { state: "detached" };
  }

  /** Observe a session's events without starting a turn (reconnect path). */
  async streamEvents(
    sessionId: string,
    onEvent: (event: LoomEvent) => void,
    { afterSeq = 0, signal }: { afterSeq?: number; signal?: AbortSignal } = {},
  ): Promise<void> {
    await this.readSse(
      this.url(
        `/sessions/${encodeURIComponent(sessionId)}/events?after_seq=${afterSeq}`,
      ),
      { headers: { ...this.headers, Accept: "text/event-stream" }, signal },
      onEvent,
    );
  }

  private async readSse(
    url: string,
    init: RequestInit,
    onEvent: (event: LoomEvent) => void,
    onAccepted?: () => void,
  ): Promise<void> {
    const response = await this.fetchImpl(url, init);
    if (!response.ok) throw await readError("Task stream failed", response);
    if (!response.body) throw new LoomProtocolError("Task response has no event stream");
    onAccepted?.();

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let reachedEof = false;

    const emit = (frame: string) => {
      let name = "message";
      const dataLines: string[] = [];
      for (const line of frame.split(/\r?\n/)) {
        if (line.startsWith(":")) continue; // comment / heartbeat
        if (line.startsWith("event:")) name = line.slice(6).trim();
        else if (line.startsWith("data:")) dataLines.push(line.slice(5).replace(/^ /, ""));
      }
      if (!dataLines.length) return;
      const raw = dataLines.join("\n");
      let payload: LoomEvent;
      try {
        const parsed = JSON.parse(raw) as Record<string, unknown>;
        payload = {
          seq: typeof parsed.seq === "number" ? parsed.seq : undefined,
          event: typeof parsed.event === "string" ? parsed.event : name,
          data: parsed.data ?? parsed,
          ts: typeof parsed.ts === "string" ? parsed.ts : undefined,
        };
      } catch {
        payload = { event: name, data: raw };
      }
      onEvent(payload);
    };

    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) {
          reachedEof = true;
          buffer += decoder.decode();
          if (buffer.trim()) emit(buffer);
          break;
        }
        buffer += decoder.decode(value, { stream: true });
        for (;;) {
          const boundary = /\r?\n\r?\n/.exec(buffer);
          if (!boundary) break;
          const frame = buffer.slice(0, boundary.index);
          buffer = buffer.slice(boundary.index + boundary[0].length);
          emit(frame);
        }
      }
    } finally {
      if (!reachedEof) await reader.cancel().catch(() => undefined);
      reader.releaseLock();
    }
  }
}
