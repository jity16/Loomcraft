# Changelog

## 0.1.0 — 2026-08-27

- Extracted a registry-neutral AI-native DAG core from the original application.
- Added strict Plan/input validation, revision history and objective coverage ledgers.
- Added bounded parallel execution, retries with exponential backoff, timeouts,
  failure policies, cancellation and approval pauses.
- Added normalized AI provider adapters (Chat Completions, Responses, JSONL and scripted).
- Added ToolBroker dynamic tool contract and optional FastAPI/SSE adapter.
- Added React/TypeScript renderer with deterministic SVG layout, event reducer,
  revision tabs, activity timeline, responsive styling and reduced-motion support.
- Added runnable examples, schemas, tests, and public integration documentation.
