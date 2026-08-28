# Changelog

All notable changes to LoomCraft. Dates are ISO-8601.

## [Unreleased]

### Added

- **Whole-plan execution.** `execute_plan` compiles a published plan into one
  `ExecutionGraph` and runs it on the existing engine: independent branches run
  concurrently, each step carries its own retry budget and timeout, and the run
  parks at approval gates. `PlanExecutor` and `build_plan_graph` are public.
- **Per-step execution policy.** `PlanStep` gains `retry`, `timeout_seconds`,
  `on_failure` and `metadata`. An omitted `retry` inherits the capability's own
  policy, so publishing a plan never downgrades a capability that asked for
  three attempts. `on_failure="continue"` lets independent dependents run past
  a failed branch, and such a failure no longer fails the whole run.
- **Objectives and an evidence ledger.** A plan may declare `objectives` — the
  questions the work must answer — each discharged by an `analysis_coverage`
  entry. `executed` requires a supporting step or artifact; `not_estimable`,
  `blocked` and `deferred_by_scope` require a `next_action`. A revision may
  reclassify an objective but may not silently drop one.
- **New step statuses** `ready`, `waiting_approval` and `cancelled`, with
  matching transitions, renderer styles and reducer support.
- **Review steps may bind a capability** whose runner starts with `review.` or
  which is tagged `review`. Such a step is server-owned and cannot be closed
  with `update_step`.
- **`Run.settled()`** returns at a terminal status *or* an approval pause;
  `Engine.execute` now uses it, so an approval-gated graph no longer deadlocks
  the caller that is meant to resolve it.
- **`SubprocessAgent`** drives a model runner in another process over JSONL on
  stdio, and `OpenAICompatibleAgent(stream=True)` emits normalized deltas and
  reassembles fragmented tool-call arguments.
- **`AppServerBridge`** translates JSON-RPC (`initialize`, `tools/list`,
  `tools/call`, `item/tool/call`) into broker calls, so a Codex app-server or
  any JSON-RPC host can drive a session without a second validation path.
  `broker.dispatch_dynamic_tool()` and `dynamic_tool_specs()` are its two ends.
- **`registry.register_step_handler(kind, fn)`** supplies how `execute_plan`
  should run `dynamic`, `review` and `answer` steps.
- `NodeResult.skip()`, `NodeContext.dependencies`, `NodeContext.adopt_artifact`,
  `Engine(max_retained_runs=…)`, `plan.diff_plans`, `plan.task_phase`.
- Generated JSON Schema for the plan, event and tool contracts, checked in CI.
- **Workbench Tour example.** An eleven-step fan-out/fan-in plan makes the
  measured parallel window, retry, artifact flow, and pre-execution approval
  gate visible without an API key.
- **README refresh.** The landing page now leads with the agent → broker →
  engine → renderer boundary and a visual workbench before the API detail.

### Fixed

- **`requires_approval` was never enforced.** The engine now parks a node
  *before* invoking its runner, so the gate sits in front of the side effect
  rather than behind it. Approval releases the node and sets
  `ctx.config["approved"]`; rejection means the work never runs.
- **Artifacts from a failed attempt became deliverables.** Emitted files are
  buffered and only registered once the attempt succeeds or parks, so a
  partial write from a retry is neither downloadable nor visible downstream.
- **`ctx.emit` accepted `../` and absolute filenames**, writing outside the
  node workdir. Filenames are now confined to it.
- **`ctx.emit` accepted undeclared output ports**, creating downstream edges no
  consumer agreed to. Ports are checked against the node's declared outputs.
- **`Session.history()` leaked `relpath`** — the host's on-disk layout — to the
  browser. `public_artifact`, `public_execution` and `public_plan` scrub host
  paths, command lines and environment out of everything that leaves the
  process.
- **Number parameters accepted NaN and infinity**, which pass every range check
  and then break strict JSON serialization.
- **`Engine._runs` grew without bound.** Terminal runs are pruned to
  `max_retained_runs`; live and paused handles are never pruned.
- **The SSE endpoint read its backlog before subscribing**, losing any event
  appended in that window. It now subscribes first and de-duplicates by
  sequence number, and the reducer ignores events at or below its watermark.
- **Upstream artifacts were bound straight from `relpath`**, bypassing
  integrity re-verification. They now resolve through `resolve_source`, and a
  capability's declared extensions are enforced on upstream files too.
- **An approval pause ended the broker with the turn**, losing the run the
  approval endpoint needed. The broker now survives a pause.
- **SVG marker ids in `PlanGraph` were static**, so two graphs on one page
  shared arrowheads. They are now scoped per instance with `useId()`.
- The reducer no longer makes an older revision current when history arrives
  out of order, and `parseExecution` hydrates per-node state instead of
  discarding it.
- Input-validation errors no longer echo the rejected values back to the model.
- `topological_order` returns the lexicographically smallest valid order, so
  declaration order cannot change the result.

## [0.1.0] — 2026-08-27

Initial release: agent-authored plans, a validated tool broker, a DAG engine
with retries and cancellation, a hash-chained event log, and a dependency-free
React renderer.
