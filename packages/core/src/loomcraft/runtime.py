"""Convenience runtime that wires the canonical Session, Broker and Agent APIs.

Applications may use the lower-level objects directly.  ``LoomcraftRuntime`` is
the small integration seam for a web process: it keeps one registry, creates an
isolated session/broker/engine, and adapts either the built-in provider agents or
the dependency-free ``AIProvider`` loop from the extracted package.
"""

from __future__ import annotations

import asyncio
from typing import Any, Mapping

from .agent import Agent
from .ai import AIProvider, PlannerAgent
from .broker import BrokerLimits, ToolBroker
from .engine import Engine
from .registry import Registry
from .server import create_app, create_router
from .store import Session, SessionStore


class _ProviderAgent:
    """Turn-protocol adapter for a normalized :class:`AIProvider`."""

    def __init__(self, provider: AIProvider, broker: ToolBroker, **options: Any) -> None:
        self._provider = provider
        self._options = dict(options)
        del broker

    async def run_turn(
        self,
        broker: ToolBroker,
        message: str,
        *,
        history: Any = None,
        on_event: Any = None,
    ) -> Any:
        agent = PlannerAgent(self._provider, broker, **self._options)
        return await agent.run(message, prior_messages=history)


class LoomcraftRuntime:
    """Own a registry/store pair and create isolated per-session brokers."""

    def __init__(
        self,
        registry: Registry | None = None,
        *,
        store: SessionStore | None = None,
        provider: AIProvider | Agent | None = None,
        max_parallel: int = 8,
        max_concurrency: int | None = None,
        limits: BrokerLimits | None = None,
        table_inspector: Any = None,
        catalog_provider: Any = None,
        knowledge_provider: Any = None,
        extra_tool_handlers: Mapping[str, Any] | None = None,
    ) -> None:
        self.registry = registry or Registry()
        self.store = store or SessionStore("./loomcraft-data")
        self.provider = provider
        self.max_parallel = max(1, int(max_concurrency or max_parallel))
        self.limits = limits
        self._broker_options = {
            "table_inspector": table_inspector,
            "catalog_provider": catalog_provider,
            "knowledge_provider": knowledge_provider,
            "extra_tool_handlers": extra_tool_handlers,
        }
        self._engines: dict[str, Engine] = {}
        self._brokers: dict[str, ToolBroker] = {}
        self._turn_locks: dict[str, asyncio.Lock] = {}

    def create_session(self, session_id: str | None = None) -> dict[str, Any]:
        return self.store.create(session_id).meta()

    def session(self, session_id: str) -> Session:
        session = self.store.get(session_id)
        if session is None:
            raise KeyError(f"session {session_id!r} does not exist")
        return session

    def broker(self, session_id: str, **overrides: Any) -> ToolBroker:
        cached = self._brokers.get(session_id)
        if cached is not None and not overrides:
            return cached
        session = self.session(session_id)
        engine = overrides.pop("engine", None) or self._engines.get(session_id)
        if engine is None:
            engine = Engine(self.registry, session, max_parallel=self.max_parallel)
            self._engines[session_id] = engine
        options = {key: value for key, value in self._broker_options.items() if value is not None}
        options.update(overrides)
        broker = ToolBroker(
            session,
            self.registry,
            engine=engine,
            limits=self.limits,
            **options,
        )
        if not overrides:
            self._brokers[session_id] = broker
        return broker

    def agent(self, session_id: str, *, provider: Any = None, **options: Any) -> Agent:
        selected = provider or self.provider
        if selected is None:
            raise RuntimeError("configure an AI provider before creating an agent")
        broker = self.broker(session_id)
        if callable(getattr(selected, "run_turn", None)):
            return selected
        if callable(getattr(selected, "complete", None)):
            return _ProviderAgent(selected, broker, **options)  # type: ignore[arg-type]
        raise TypeError("provider must implement complete() or run_turn()")

    async def run_turn(self, session_id: str, message: str, *, provider: Any = None, **options: Any) -> Any:
        lock = self._turn_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            agent = self.agent(session_id, provider=provider, **options)
            return await agent.run_turn(self.broker(session_id), message)

    async def delete_session(self, session_id: str) -> bool:
        broker = self._brokers.pop(session_id, None)
        if broker is not None:
            await broker.close()
        self._engines.pop(session_id, None)
        self._turn_locks.pop(session_id, None)
        return self.store.delete(session_id)

    def router(self, *, prefix: str = "/api/v1/loomcraft", **kwargs: Any) -> Any:
        return create_router(
            self.store,
            self.registry,
            lambda session: self.agent(session.id, **kwargs),
            prefix=prefix,
            limits=self.limits,
            broker_options={key: value for key, value in self._broker_options.items() if value is not None},
        )

    def app(self, *, prefix: str = "/api/v1/loomcraft", **kwargs: Any) -> Any:
        title = str(kwargs.pop("title", "LoomCraft"))
        cors_origins = kwargs.pop("cors_origins", None)
        return create_app(
            self.store,
            self.registry,
            lambda session: self.agent(session.id, **kwargs),
            title=title,
            prefix=prefix,
            limits=self.limits,
            cors_origins=cors_origins,
            broker_options={key: value for key, value in self._broker_options.items() if value is not None},
        )


def create_fastapi_router(runtime: LoomcraftRuntime, prefix: str = "/api/v1/loomcraft") -> Any:
    """Compatibility name for the extracted runtime's FastAPI adapter."""
    return runtime.router(prefix=prefix)


__all__ = ["LoomcraftRuntime", "create_fastapi_router"]
