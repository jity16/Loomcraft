"""An optional FastAPI surface: sessions, uploads, SSE turns, artifact download.

This is a reference host, not a requirement — everything here is a thin wrapper
over :class:`~loomcraft.broker.ToolBroker` and
:class:`~loomcraft.store.SessionStore`, so embedding LoomCraft in Django, Litestar,
or a queue worker means re-implementing ~200 lines against the same objects.

The design decision worth copying: **a turn runs in the background and the SSE
response only subscribes to it.**  A client that navigates away or loses its
connection stops receiving events; it does not cancel the work.  Reconnecting
with ``?after_seq=`` replays what was missed from the durable log, so a refresh
mid-run rejoins cleanly instead of restarting.

Install the extra to use this module::

    pip install "loomcraft[server] @ git+https://github.com/jity16/Loomcraft.git#subdirectory=packages/core"
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any, Callable, Mapping

from .agent import Agent, EventSink
from .broker import BrokerLimits, ToolBroker
from .errors import LoomCraftError
from .events import Event
from .registry import Registry
from .store import Session, SessionStore, public_artifact

logger = logging.getLogger("loomcraft.server")

try:  # pragma: no cover - exercised only when the extra is installed
    from fastapi import APIRouter, Body, FastAPI, HTTPException, Query, Request, UploadFile
    from fastapi.responses import FileResponse, StreamingResponse

    FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover - keeps core import-safe without FastAPI
    FASTAPI_AVAILABLE = False
    APIRouter = Body = FastAPI = HTTPException = Query = Request = UploadFile = Any  # type: ignore[assignment,misc]
    FileResponse = StreamingResponse = Any  # type: ignore[assignment,misc]


HEARTBEAT_SECONDS = 15.0


class TurnManager:
    """Owns one background turn per session and fans its events out to viewers.

    Two jobs: keep at most one turn in flight per session (a second POST while
    one is running is a client bug, not a queue), and let any number of SSE
    connections observe a turn without owning its lifetime.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._brokers: dict[str, ToolBroker] = {}

    def is_busy(self, session_id: str) -> bool:
        task = self._tasks.get(session_id)
        if task is not None and not task.done():
            return True
        broker = self._brokers.get(session_id)
        active = broker.active_run if broker is not None else None
        return active is not None and active.status not in {
            "succeeded",
            "failed",
            "cancelled",
        }

    def broker(self, session_id: str) -> ToolBroker | None:
        return self._brokers.get(session_id)

    def start(
        self,
        session: Session,
        broker: ToolBroker,
        agent: Agent,
        message: str,
        *,
        on_event: EventSink,
    ) -> asyncio.Task[Any]:
        if self.is_busy(session.id):
            raise LoomCraftError(f"session {session.id} already has a running turn")

        self._brokers[session.id] = broker

        async def run() -> Any:
            session.update_meta(status="running")
            try:
                result = await agent.run_turn(broker, message, on_event=on_event)
                paused = (
                    broker.active_run is not None
                    and broker.active_run.status == "paused_approval"
                )
                if paused:
                    session.update_meta(
                        status="waiting_approval",
                        last_turn_status="waiting_approval",
                    )
                elif result.error:
                    session.emit("error", {"message": result.error})
                    session.update_meta(status="idle", last_turn_status="error")
                else:
                    session.update_meta(status="idle", last_turn_status="succeeded")
                session.emit("done", {"ok": result.error is None})
                return result
            except asyncio.CancelledError:
                session.emit("notice", {"message": "turn cancelled"})
                session.update_meta(status="idle", last_turn_status="cancelled")
                session.emit("done", {"ok": False, "cancelled": True})
                raise
            except Exception as exc:  # noqa: BLE001 - turn boundary
                logger.exception("turn crashed for session %s", session.id)
                session.emit("error", {"message": f"{type(exc).__name__}: {exc}"})
                session.update_meta(status="idle", last_turn_status="error")
                session.emit("done", {"ok": False})
                raise
            finally:
                # An approval pause deliberately outlives the model turn. The
                # manager retains this broker so the approval endpoint can
                # resume the exact Run instead of reconstructing state.
                if broker.active_run is None or broker.active_run.status != "paused_approval":
                    await broker.close()

        task = asyncio.create_task(run(), name=f"loomcraft-turn-{session.id}")
        self._tasks[session.id] = task
        return task

    async def cancel(self, session_id: str) -> bool:
        task = self._tasks.get(session_id)
        broker = self._brokers.get(session_id)
        cancelled = False
        if broker is not None:
            active = broker.active_run
            cancelled = bool(
                active is not None
                and active.status not in {"succeeded", "failed", "cancelled"}
            )
            await broker.close()
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            cancelled = True
        self._tasks.pop(session_id, None)
        self._brokers.pop(session_id, None)
        return cancelled

    async def shutdown(self) -> None:
        for session_id in list(self._tasks):
            await self.cancel(session_id)


def create_router(
    store: SessionStore,
    registry: Registry,
    agent_factory: Callable[[Session], Agent],
    *,
    prefix: str = "/api/v1/loomcraft",
    limits: BrokerLimits | None = None,
    manager: TurnManager | None = None,
    broker_options: Mapping[str, Any] | None = None,
) -> Any:
    """Build the LoomCraft API router.

    ``agent_factory`` receives the session and returns the agent to drive it, so
    a host can vary model, effort, or system prompt per task.
    """
    if not FASTAPI_AVAILABLE:  # pragma: no cover
        raise RuntimeError(
            "loomcraft.server needs FastAPI: "
            'pip install "loomcraft[server] @ git+https://github.com/jity16/Loomcraft.git#subdirectory=packages/core"'
        )

    router = APIRouter(prefix=prefix, tags=["loomcraft"])
    turns = manager or TurnManager()
    router.state_turns = turns  # type: ignore[attr-defined]

    def require(session_id: str) -> Session:
        session = store.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        return session

    def broker_for(session: Session) -> ToolBroker:
        existing = turns.broker(session.id)
        if existing is not None:
            return existing
        broker = ToolBroker(
            session,
            registry,
            limits=limits,
            **dict(broker_options or {}),
        )
        # Keep direct tool calls addressable by the approval/cancellation
        # aliases even when no /turn request created the TurnManager entry.
        turns._brokers[session.id] = broker
        return broker

    # ── Sessions ────────────────────────────────────────────────────────────

    @router.post("/sessions")
    async def create_session() -> dict[str, Any]:
        session = store.create()
        return session.meta()

    @router.get("/sessions")
    async def list_sessions() -> dict[str, Any]:
        return {"sessions": store.list_ids()}

    @router.get("/sessions/{session_id}")
    async def get_session(session_id: str) -> dict[str, Any]:
        return require(session_id).meta()

    @router.get("/sessions/{session_id}/history")
    async def get_history(
        session_id: str, after_seq: int = Query(default=0, ge=0)
    ) -> dict[str, Any]:
        return require(session_id).history(after_seq=after_seq)

    @router.delete("/sessions/{session_id}")
    async def delete_session(session_id: str) -> dict[str, Any]:
        await turns.cancel(session_id)
        return {"deleted": store.delete(session_id)}

    # ── Catalog ─────────────────────────────────────────────────────────────

    @router.get("/catalog")
    async def catalog() -> dict[str, Any]:
        return {
            "capabilities": [
                item.contract() if callable(getattr(item, "contract", None)) else item.to_catalog()
                for item in registry.capabilities.values()
            ],
            "workflows": [
                item.contract() if callable(getattr(item, "contract", None)) else item.to_catalog()
                for item in registry.workflows.values()
            ],
        }

    @router.get("/tools")
    async def tools() -> dict[str, Any]:
        """Compatibility discovery endpoint for the extracted runtime."""
        from .tools import dynamic_tool_specs

        return {"tools": dynamic_tool_specs()}

    @router.get("/sessions/{session_id}/context")
    async def context(session_id: str) -> dict[str, Any]:
        session = require(session_id)
        broker = broker_for(session)
        broker.begin_turn()
        return (await broker.dispatch("session_context")).to_dict()

    @router.post("/sessions/{session_id}/tools/{tool_name}")
    async def dispatch_tool(
        session_id: str,
        tool_name: str,
        payload: dict[str, Any] = Body(default={}),
    ) -> dict[str, Any]:
        session = require(session_id)
        broker = broker_for(session)
        broker.begin_turn()
        return (await broker.dispatch(tool_name, payload)).to_dict()

    # ── Uploads ─────────────────────────────────────────────────────────────

    @router.post("/sessions/{session_id}/uploads")
    async def upload(session_id: str, file: UploadFile) -> dict[str, Any]:
        session = require(session_id)
        try:
            return session.save_upload(
                file.filename or "upload",
                file.file,
                content_type=file.content_type,
            )
        except LoomCraftError as exc:
            raise HTTPException(status_code=400, detail=exc.public_message) from exc

    @router.delete("/sessions/{session_id}/uploads/{upload_id}")
    async def delete_upload(session_id: str, upload_id: str) -> dict[str, Any]:
        session = require(session_id)
        removed = session.delete_upload(upload_id)
        if removed is None:
            raise HTTPException(status_code=404, detail="upload not found")
        # A deleted file may have satisfied a request the agent is relying on;
        # re-open those so it asks again rather than running on a missing input.
        invalidated = broker_for(session).invalidate_requests_for_upload(upload_id)
        return {"deleted": upload_id, "invalidated_request_ids": invalidated}

    # ── Turns ───────────────────────────────────────────────────────────────

    @router.post("/sessions/{session_id}/turn")
    async def turn(
        session_id: str,
        request: Request,
        payload: dict[str, Any] = Body(...),
    ) -> Any:
        session = require(session_id)
        message = str(payload.get("message") or "").strip()
        if not message:
            raise HTTPException(status_code=400, detail="message is required")
        if turns.is_busy(session_id):
            raise HTTPException(status_code=409, detail="a turn is already running")

        queue: asyncio.Queue[Event | None] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        start_seq = session.events.last_seq

        def forward(event: Event) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, event)

        unsubscribe = session.events.subscribe(forward)
        broker = ToolBroker(
            session,
            registry,
            limits=limits,
            **dict(broker_options or {}),
        )

        def sink(name: str, data: Mapping[str, Any]) -> None:
            # Streaming-only frames (token deltas, tool call markers) are not
            # persisted; push them straight at the viewer.
            if name in {"message_delta", "tool_call", "tool_result"}:
                loop.call_soon_threadsafe(
                    queue.put_nowait,
                    Event(seq=-1, event=name, data=dict(data), ts=""),
                )

        task = turns.start(session, broker, agent_factory(session), message, on_event=sink)
        task.add_done_callback(
            lambda _: loop.call_soon_threadsafe(queue.put_nowait, None)
        )

        async def stream() -> Any:
            try:
                yield f": subscribed at seq {start_seq}\n\n"
                while True:
                    try:
                        event = await asyncio.wait_for(
                            queue.get(), timeout=HEARTBEAT_SECONDS
                        )
                    except asyncio.TimeoutError:
                        # Keep proxies from closing an idle long-running turn.
                        yield ": heartbeat\n\n"
                        continue
                    if event is None:
                        break
                    if await request.is_disconnected():
                        break
                    yield event.sse()
            finally:
                # Detaching a viewer must never cancel the turn.
                unsubscribe()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    @router.get("/sessions/{session_id}/events")
    async def stream_events(
        session_id: str,
        request: Request,
        after_seq: int = Query(default=0, ge=0),
        after: int | None = Query(default=None, ge=0),
    ) -> Any:
        """Attach to a session's event stream without starting a turn."""
        session = require(session_id)
        queue: asyncio.Queue[Event] = asyncio.Queue()
        loop = asyncio.get_running_loop()
        if after is not None:
            after_seq = after
        unsubscribe = session.events.subscribe(
            lambda event: loop.call_soon_threadsafe(queue.put_nowait, event)
        )
        # Subscribe before reading the backlog so an append racing this attach
        # cannot fall into the gap. ``last_sent`` below removes the overlap.
        backlog = session.events.read(after_seq=after_seq)

        async def stream() -> Any:
            last_sent = after_seq
            try:
                for event in backlog:
                    if event.seq > last_sent:
                        yield event.sse()
                        last_sent = event.seq
                while not await request.is_disconnected():
                    try:
                        event = await asyncio.wait_for(
                            queue.get(), timeout=HEARTBEAT_SECONDS
                        )
                    except asyncio.TimeoutError:
                        yield ": heartbeat\n\n"
                        continue
                    if event.seq > last_sent:
                        yield event.sse()
                        last_sent = event.seq
            finally:
                unsubscribe()

        return StreamingResponse(stream(), media_type="text/event-stream")

    @router.post("/sessions/{session_id}/cancel")
    async def cancel_turn(session_id: str) -> dict[str, Any]:
        require(session_id)
        return {"cancelled": await turns.cancel(session_id)}

    @router.post("/sessions/{session_id}/runs/{run_id}/cancel")
    async def cancel_run_alias(session_id: str, run_id: str) -> dict[str, Any]:
        session = require(session_id)
        broker = turns.broker(session.id) or broker_for(session)
        return {"cancelled": await broker.cancel_run(run_id), "run_id": run_id}

    # ── Input requests ──────────────────────────────────────────────────────

    @router.post("/sessions/{session_id}/input-requests/{request_id}/fulfill")
    async def fulfill(session_id: str, request_id: str) -> dict[str, Any]:
        session = require(session_id)
        try:
            return broker_for(session).fulfill_input_request(request_id)
        except LoomCraftError as exc:
            raise HTTPException(status_code=400, detail=exc.public_message) from exc

    @router.post("/sessions/{session_id}/input-requests/{request_id}/cancel")
    async def cancel_request(session_id: str, request_id: str) -> dict[str, Any]:
        session = require(session_id)
        try:
            return broker_for(session).cancel_input_request(request_id)
        except LoomCraftError as exc:
            raise HTTPException(status_code=400, detail=exc.public_message) from exc

    @router.post("/sessions/{session_id}/inputs/{request_id}/fulfill")
    async def fulfill_alias(
        session_id: str,
        request_id: str,
        payload: dict[str, Any] = Body(default={}),
    ) -> dict[str, Any]:
        session = require(session_id)
        broker = broker_for(session)
        uploads = payload.get("uploads")
        if uploads is None:
            return broker.fulfill_input_request(request_id)
        return {
            "request_id": request_id,
            "allocation": broker.fulfill_inputs(request_id, uploads),
        }

    @router.post("/sessions/{session_id}/inputs/{request_id}/cancel")
    async def cancel_request_alias(session_id: str, request_id: str) -> dict[str, Any]:
        session = require(session_id)
        broker_for(session).cancel_input_request(request_id)
        return {"request_id": request_id, "cancelled": True}

    # ── Approvals ───────────────────────────────────────────────────────────

    @router.post("/sessions/{session_id}/executions/{run_id}/approve")
    async def approve(
        session_id: str,
        run_id: str,
        payload: dict[str, Any] = Body(default={}),
    ) -> dict[str, Any]:
        session = require(session_id)
        node_id = str(payload.get("node_id") or "")
        approved = bool(payload.get("approved", True))
        broker = turns.broker(session.id) or broker_for(session)
        result = await broker.approve_run(
            run_id, node_id, approved=approved, comment=str(payload.get("comment") or "")
        )
        if result is None:
            raise HTTPException(status_code=409, detail="node is not awaiting approval")
        return {"run_id": run_id, "node_id": node_id, "approved": approved}

    @router.post("/sessions/{session_id}/runs/{run_id}/approve")
    async def approve_alias(
        session_id: str,
        run_id: str,
        payload: dict[str, Any] = Body(default={}),
    ) -> dict[str, Any]:
        session = require(session_id)
        node_id = str(payload.get("node_id") or payload.get("step_id") or "")
        if not node_id:
            raise HTTPException(status_code=400, detail="node_id is required")
        broker = turns.broker(session.id) or broker_for(session)
        result = await broker.approve_run(
            run_id,
            node_id,
            approved=payload.get("approved", True) is not False,
            comment=str(payload.get("comment") or ""),
        )
        if result is None:
            raise HTTPException(status_code=409, detail="node is not awaiting approval")
        return {
            "run_id": run_id,
            "node_id": node_id,
            "approved": payload.get("approved", True) is not False,
        }

    # ── Artifacts ───────────────────────────────────────────────────────────

    @router.get("/sessions/{session_id}/artifacts")
    async def list_artifacts(session_id: str) -> dict[str, Any]:
        return {
            "artifacts": [
                public_artifact(item) for item in require(session_id).list_artifacts()
            ]
        }

    @router.get("/sessions/{session_id}/artifacts/{artifact_id}")
    async def download_artifact(session_id: str, artifact_id: str) -> Any:
        session = require(session_id)
        found = session.get_artifact(artifact_id)
        if found is None:
            raise HTTPException(status_code=404, detail="artifact not found")
        row, path = found
        return FileResponse(
            path,
            media_type=str(row.get("content_type", "application/octet-stream")),
            filename=str(row.get("filename", "artifact")),
        )

    return router


def create_app(
    store: SessionStore,
    registry: Registry,
    agent_factory: Callable[[Session], Agent],
    *,
    title: str = "LoomCraft",
    prefix: str = "/api/v1/loomcraft",
    limits: BrokerLimits | None = None,
    cors_origins: list[str] | None = None,
    broker_options: Mapping[str, Any] | None = None,
) -> Any:
    """A ready-to-serve FastAPI app — handy for demos and the examples."""
    if not FASTAPI_AVAILABLE:  # pragma: no cover
        raise RuntimeError(
            "loomcraft.server needs FastAPI: "
            'pip install "loomcraft[server] @ git+https://github.com/jity16/Loomcraft.git#subdirectory=packages/core"'
        )
    app = FastAPI(title=title, version="0.1.0")
    manager = TurnManager()
    app.include_router(
        create_router(
            store,
            registry,
            agent_factory,
            prefix=prefix,
            limits=limits,
            manager=manager,
            broker_options=broker_options,
        )
    )

    if cors_origins:
        from fastapi.middleware.cors import CORSMiddleware  # noqa: PLC0415

        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.on_event("shutdown")
    async def _shutdown() -> None:  # pragma: no cover - lifecycle hook
        await manager.shutdown()

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        problems = registry.validate()
        return {
            "ok": not problems,
            "problems": problems,
            "capabilities": len(registry.capabilities),
            "workflows": len(registry.workflows),
        }

    return app


def sse_payload(event: Event) -> str:
    """Render an event as an SSE frame (exposed for custom hosts)."""
    return event.sse()


def json_line(event: Event) -> str:
    """Render an event as one NDJSON line (for non-SSE transports)."""
    return json.dumps(event.to_dict(), ensure_ascii=False) + "\n"


def create_fastapi_router(runtime: Any, prefix: str = "/api/v1/loomcraft") -> Any:
    """Lazy compatibility bridge to :class:`loomcraft.runtime.LoomcraftRuntime`."""
    router_factory = getattr(runtime, "router", None)
    if not callable(router_factory):
        raise TypeError("runtime must expose router(prefix=...)")
    return router_factory(prefix=prefix)


__all__ = [
    "FASTAPI_AVAILABLE",
    "TurnManager",
    "create_app",
    "create_router",
    "create_fastapi_router",
    "json_line",
    "sse_payload",
]
