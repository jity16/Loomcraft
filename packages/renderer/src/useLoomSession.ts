/**
 * `useLoomSession` — the one hook most hosts need.
 *
 * Owns a session's reducer state, the SSE subscription, and the actions the UI
 * fires (send, cancel, upload, fulfil, approve, download). Hosts that want their
 * own state container can skip this and use `reduceLoomEvent` + `LoomClient`
 * directly; everything here is composed from those two.
 *
 * Reconnect behaviour worth noting: if a turn's stream detaches (tab slept, wifi
 * dropped, proxy timed out) the turn keeps running on the server. The hook
 * re-attaches to the event stream from `lastSeq`, so the user sees the run
 * continue rather than a stalled UI or a duplicated task.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { LoomClient, type LoomClientOptions } from "./client";
import {
  appendUserMessage,
  deriveTaskPhase,
  hydrateLoomState,
  initialLoomState,
  orientationActivities,
  pendingInputRequests,
  reduceLoomEvent,
} from "./state";
import type { Artifact, LoomState, Plan } from "./types";

export interface UseLoomSessionOptions extends LoomClientOptions {
  sessionId: string | null;
  /** Load `/history` on mount so a refresh restores the task. Default: true. */
  hydrate?: boolean;
  /** Re-attach to the event stream when a turn's connection drops. Default: true. */
  reattachOnDetach?: boolean;
  client?: LoomClient;
}

export interface UseLoomSession {
  state: LoomState;
  busy: boolean;
  phase: ReturnType<typeof deriveTaskPhase>;
  /** The revision being viewed — the live one unless the user picked another. */
  visiblePlan: Plan | null;
  selectedRevision: number | null;
  selectRevision: (revision: number | null) => void;
  selectedStepId: string | null;
  selectStep: (stepId: string | null) => void;
  orientation: ReturnType<typeof orientationActivities>;
  pendingRequests: ReturnType<typeof pendingInputRequests>;
  error: string | null;
  send: (message: string) => Promise<void>;
  cancel: () => Promise<void>;
  upload: (files: FileList | File[]) => Promise<void>;
  deleteUpload: (uploadId: string) => Promise<void>;
  fulfillRequest: (requestId: string) => Promise<void>;
  cancelRequest: (requestId: string) => Promise<void>;
  approve: (runId: string, nodeId: string, approved: boolean) => Promise<void>;
  download: (artifact: Artifact) => Promise<void>;
  refresh: () => Promise<void>;
  client: LoomClient;
}

export function useLoomSession(options: UseLoomSessionOptions): UseLoomSession {
  const {
    sessionId,
    hydrate = true,
    reattachOnDetach = true,
    client: providedClient,
    ...clientOptions
  } = options;

  const client = useMemo(
    () => providedClient ?? new LoomClient(clientOptions),
    // Client identity should follow its configuration, not every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [providedClient, clientOptions.baseUrl, JSON.stringify(clientOptions.headers ?? {})],
  );

  const [state, setState] = useState<LoomState>(initialLoomState);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [selectedRevision, setSelectedRevision] = useState<number | null>(null);
  const [selectedStepId, setSelectedStepId] = useState<string | null>(null);

  const abortRef = useRef<AbortController | null>(null);
  const stateRef = useRef(state);
  stateRef.current = state;

  const apply = useCallback((event: Parameters<typeof reduceLoomEvent>[1]) => {
    setState((current) => reduceLoomEvent(current, event));
  }, []);

  const refresh = useCallback(async () => {
    if (!sessionId) return;
    try {
      const history = await client.getHistory(sessionId);
      setState(hydrateLoomState(history));
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    }
  }, [client, sessionId]);

  useEffect(() => {
    setState(initialLoomState);
    setSelectedRevision(null);
    setSelectedStepId(null);
    setError(null);
    if (sessionId && hydrate) void refresh();
  }, [sessionId, hydrate, refresh]);

  // Cancel any in-flight stream when the component unmounts.
  useEffect(() => () => abortRef.current?.abort(), []);

  const attach = useCallback(async () => {
    if (!sessionId) return;
    const controller = new AbortController();
    abortRef.current = controller;
    try {
      await client.streamEvents(sessionId, apply, {
        afterSeq: stateRef.current.lastSeq,
        signal: controller.signal,
      });
    } catch {
      // Losing a passive observer stream is not a task failure.
    }
  }, [apply, client, sessionId]);

  const send = useCallback(
    async (message: string) => {
      const trimmed = message.trim();
      if (!sessionId || !trimmed || busy) return;
      setError(null);
      setBusy(true);
      setState((current) => appendUserMessage(current, trimmed));

      const controller = new AbortController();
      abortRef.current = controller;
      try {
        const outcome = await client.runTurn(
          sessionId,
          trimmed,
          apply,
          controller.signal,
        );
        if (outcome.state === "detached" && reattachOnDetach) {
          // The turn is still running server-side; rejoin it.
          await refresh();
          await attach();
        }
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : String(cause));
      } finally {
        setBusy(false);
        abortRef.current = null;
      }
    },
    [apply, attach, busy, client, reattachOnDetach, refresh, sessionId],
  );

  const cancel = useCallback(async () => {
    if (!sessionId) return;
    try {
      await client.cancelTurn(sessionId);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      abortRef.current?.abort();
      setBusy(false);
    }
  }, [client, sessionId]);

  const upload = useCallback(
    async (files: FileList | File[]) => {
      if (!sessionId) return;
      const list = Array.from(files as ArrayLike<File>);
      try {
        const uploaded = await Promise.all(
          list.map((file) => client.uploadFile(sessionId, file)),
        );
        setState((current) => ({
          ...current,
          uploads: [...current.uploads, ...uploaded],
        }));
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : String(cause));
      }
    },
    [client, sessionId],
  );

  const deleteUpload = useCallback(
    async (uploadId: string) => {
      if (!sessionId) return;
      try {
        await client.deleteUpload(sessionId, uploadId);
        setState((current) => ({
          ...current,
          uploads: current.uploads.filter((item) => item.id !== uploadId),
        }));
        await refresh();
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : String(cause));
      }
    },
    [client, refresh, sessionId],
  );

  const fulfillRequest = useCallback(
    async (requestId: string) => {
      if (!sessionId) return;
      try {
        await client.fulfillInputRequest(sessionId, requestId);
        await refresh();
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : String(cause));
      }
    },
    [client, refresh, sessionId],
  );

  const cancelRequest = useCallback(
    async (requestId: string) => {
      if (!sessionId) return;
      try {
        await client.cancelInputRequest(sessionId, requestId);
        await refresh();
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : String(cause));
      }
    },
    [client, refresh, sessionId],
  );

  const approve = useCallback(
    async (runId: string, nodeId: string, approved: boolean) => {
      if (!sessionId) return;
      try {
        await client.approveNode(sessionId, runId, nodeId, approved);
        // The model turn ended at the gate, so there is no live POST stream to
        // carry the resumed run's events. Re-read the durable history once the
        // approval endpoint returns at the next gate or terminal state.
        await refresh();
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : String(cause));
      }
    },
    [client, refresh, sessionId],
  );

  const download = useCallback(
    async (artifact: Artifact) => {
      if (!sessionId) return;
      try {
        await client.downloadArtifact(sessionId, artifact.id, artifact.filename);
      } catch (cause) {
        setError(cause instanceof Error ? cause.message : String(cause));
      }
    },
    [client, sessionId],
  );

  const visiblePlan = useMemo(() => {
    if (selectedRevision === null) return state.currentPlan;
    return (
      state.plans.find((plan) => plan.revision === selectedRevision) ?? state.currentPlan
    );
  }, [selectedRevision, state.currentPlan, state.plans]);

  return {
    state,
    busy,
    phase: deriveTaskPhase(state, busy),
    visiblePlan,
    selectedRevision,
    selectRevision: setSelectedRevision,
    selectedStepId,
    selectStep: setSelectedStepId,
    orientation: orientationActivities(state.timeline),
    pendingRequests: pendingInputRequests(state),
    error: error ?? state.error,
    send,
    cancel,
    upload,
    deleteUpload,
    fulfillRequest,
    cancelRequest,
    approve,
    download,
    refresh,
    client,
  };
}
