# Defining plans

The plan schema, every validation rule, the step state machine, and how to
replan. This is the reference for what the server will and will not accept.

- [Schema](#schema)
- [Validation rules](#validation-rules)
- [The step state machine](#the-step-state-machine)
- [Dependency gating](#dependency-gating)
- [Skip propagation](#skip-propagation)
- [Replanning](#replanning)
- [Working with plans in code](#working-with-plans-in-code)
- [Designing good plans](#designing-good-plans)
- [Error reference](#error-reference)

---

## Schema

```jsonc
{
  "goal": "string, 1–2000 chars, required",
  "summary": "string, ≤2000 chars, optional",
  "revision": 1,                      // integer 1–1,000,000, required, must increase
  "reason": null,                     // required when replacing a plan
  "steps": [                          // 1–256 steps (24 recommended for readability)
    {
      "id": "clean",                  // ^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$
      "title": "Clean the table",     // 1–160 chars
      "kind": "capability",           // answer|capability|workflow|dynamic|review
      "depends_on": [],               // ≤256 step ids, must all exist
      "capability": "gwas.qc",      // required for capability|workflow; optional for review-only capability
      "description": ""               // ≤1000 chars, optional
    }
  ]
}
```

Steps also carry `status`, `summary`, and `execution`. Those are **server-owned**:
a model may send them, and `validate_plan` throws them away.

```python
validated = validate_plan({
    "goal": "g", "revision": 1,
    "steps": [{"id": "a", "title": "A", "kind": "dynamic",
               "status": "succeeded", "summary": "already done, honest"}],
})
validated["steps"][0]["status"]   # "pending"
validated["steps"][0]["summary"]  # None
```

---

## Validation rules

`validate_plan(raw, current=None, registry=None)` enforces all of these and
raises `PlanValidationError` on the first violation.

### Structure

| Rule | Rejected example |
| --- | --- |
| Step ids are unique | two steps both called `load` |
| Every `depends_on` target exists | `depends_on: ["ghost"]` |
| No self-dependency | `{"id": "a", "depends_on": ["a"]}` |
| No duplicate dependencies | `depends_on: ["a", "a"]` |
| The graph is acyclic | `a → b → c → a` |
| 1–256 steps | 257 steps |
| Ids match the id pattern | `"my step"`, `"../etc"` |

Cycle detection uses an iterative colouring DFS, so a deep graph cannot exhaust
the recursion limit, and the error names the cycle:

```python
validate_plan({"goal": "g", "revision": 1, "steps": [
    {"id": "a", "title": "A", "kind": "dynamic", "depends_on": ["b"]},
    {"id": "b", "title": "B", "kind": "dynamic", "depends_on": ["a"]},
]})
# PlanValidationError: plan: b: cycle detected: b -> a -> b
```

### Kind agreement

- `capability` and `workflow` steps **must** declare a `capability`.
- `review` may declare a capability only when its runner is explicitly review-scoped
  (`review.*`, a `review` tag, or `metadata.step_kinds`).
- `answer` and `dynamic` **must not** declare a capability.

```python
{"id": "a", "title": "A", "kind": "dynamic", "capability": "gwas.qc"}
# PlanValidationError: dynamic step cannot declare a capability
```

### Registry agreement (optional but recommended)

Pass a `registry` and every `capability`/`workflow` step is checked against the
catalog at publish time:

```python
validate_plan(plan, current, registry=registry)
# PlanValidationError: assoc: unknown capability 'gwas.associat'
```

`ToolBroker` always does this. Catching a typo at publish time turns a
mid-execution surprise into an immediately correctable error — the agent still
has the plan in context and fixes it in the same turn.

### Value-free errors

`PlanValidationError` carries two messages: `str(exc)` for your logs and
`exc.public_message` for the model. The public one names the field and the
problem but never echoes the rejected value.

```python
try:
    validate_plan(plan)
except PlanValidationError as exc:
    logger.warning("plan rejected: %s", exc)      # full detail
    tell_the_model(exc.public_message)            # bounded, no values
```

This matters more than it looks. A model handed its own bad payload back tends to
resend it; a model told `steps[2].title: too long` fixes it.

---

## The step state machine

```
                  ┌──────────────────────────────┐
                  ▼                              │
   pending ──► running ──► succeeded  (terminal) │
      │           │                              │
      │           ├──► failed ───────────────────┤  retry in place
      │           └──► skipped ──────────────────┘
      │                  ▲
      └──────────────────┘
```

| From | May go to |
| --- | --- |
| `pending` | `pending`, `running`, `waiting_approval`, `succeeded`, `failed`, `skipped`, `cancelled` |
| `ready` | `ready`, `running`, `skipped`, `cancelled` |
| `running` | `running`, `waiting_approval`, `succeeded`, `failed`, `skipped`, `cancelled` |
| `waiting_approval` | `waiting_approval`, `running`, `succeeded`, `failed`, `cancelled` |
| `succeeded` | `succeeded` — **terminal** |
| `failed` | `failed`, `running` — retry without a replan |
| `skipped` | `skipped`, `running` — revive |
| `cancelled` | `cancelled`, `running` — resume after an explicit retry |

Two deliberate choices:

**`succeeded` is terminal.** Once a step has produced verified artifacts, nothing
may rewrite that. Downstream steps and the final answer depend on it.

**`failed → running` is allowed.** A step can be retried in place after fixing
the inputs, without forcing a full replan. Use it when *the approach was right
and something incidental went wrong*; replan when the approach itself was wrong.

```python
state = update_step(plan, "clean", "running")
state = update_step(state, "clean", "succeeded", summary="12 rows kept")
update_step(state, "clean", "failed")
# StepTransitionError: invalid step transition 'succeeded' -> 'failed'
```

Every write re-validates the whole plan, so a state update can never leave a plan
the engine or the renderer would refuse to load.

---

## Dependency gating

A step may not start until **all** of its dependencies are `succeeded`. Not
"terminal" — succeeded. If a failed or skipped dependency declares
`on_failure: "continue"`, its direct dependents may still run; otherwise they
are skipped.

```python
ensure_dependencies_succeeded(plan, "report")
# DependencyError: step 'report' has incomplete dependencies: profile, outliers
```

`ensure_step_startable` bundles the full precondition set used before execution:

```python
ensure_step_startable(plan, "qc", kind="capability", capability="gwas.qc")
```

1. The step exists.
2. Its `kind` matches.
3. Its `capability` matches — an agent cannot point a `gwas.qc` step at
   `admin.delete_everything`.
4. Dependencies have all succeeded.
5. Status is `pending` — it has not already run.

Query the frontier from either side:

```python
parsed = parse_plan(plan)
parsed.ready_steps()     # startable right now
parsed.blocked_steps()   # pending, but a stop-policy upstream failed/skipped
parsed.layers            # [['clean'], ['profile', 'outliers'], ['report']]
parsed.progress          # {'pending': 2, 'succeeded': 1, ..., 'total': 5}
parsed.is_complete       # every step terminal
```

---

## Skip propagation

When a step fails with the default `on_failure: "stop"`, everything downstream
is marked `skipped` — transitively, to a fixed point:

```python
state = update_step(plan, "clean", "failed")
state = propagate_skips(state)
# clean: failed, profile: skipped, outliers: skipped, report: skipped
```

Sibling branches are unaffected:

```
        clean (failed)          fetch_reference (succeeded)
        ╱          ╲                     │
   profile        outliers               │
   (skipped)      (skipped)              │
        ╲          ╱                     │
          report (skipped)  ◄────────────┘
```

`ToolBroker` calls this automatically after a stop-policy failure, so the plan
reflects reality without the agent having to walk the graph; continue-policy
branches remain pending and may be started once their other dependencies are
ready.

---

## Replanning

### The rules

1. `revision` must be **strictly greater** than the current one.
2. A revision replacing an existing plan must carry a non-empty `reason`.
3. You cannot replace a plan while any step is `running` or `waiting_approval`.
4. Old revisions are retained in `session.plan_history()`.

```python
await broker.dispatch("publish_plan", {"plan": {
    "goal": "Produce a comparative brief.",
    "revision": 2,
    "reason": ("Contradiction analysis needs at least two documents and the "
               "user declined to supply a second. Dropping the cross-check "
               "step and delivering a single-document brief instead."),
    "steps": [...],
}})
```

### What a good reason looks like

The `reason` is not ceremony — it is the audit trail a reviewer reads six weeks
later, and it is also what a *future turn of the same agent* reads to avoid
repeating a dead end.

| ✗ Weak | ✓ Useful |
| --- | --- |
| "Adjusting the plan" | "The gene catalogue returned 503 on all three attempts; reporting the loci without annotation." |
| "Step failed, trying again" | "`gwas.qc` dropped every marker at MAF ≥ 0.05. Re-running QC at 0.01 and recording the looser threshold in the summary." |
| "Changed approach" | "λ = 2.80 in revision 1: the statistics are inflated genome-wide, which is population structure rather than signal. Adding ancestry axes and a relatedness matrix, and rescanning with a mixed model." |

State **what was learned** and **what changes because of it**.

### Replan vs retry

| Situation | Do this |
| --- | --- |
| Transient failure (503, timeout, lock) | Let the runner's `max_attempts` handle it |
| Wrong inputs, right approach | `failed → running` and re-run the step |
| The approach cannot work | **Replan** with a `reason` |
| Missing information | `request_inputs`, then replan if it does not arrive |
| Scope should shrink | **Replan** with a reduced plan and say why |

### Artifacts survive

Publishing a new revision resets step *statuses*, not the session. Artifacts
produced under revision 1 remain resolvable under revision 2, so an agent that
already extracted a corpus reuses it rather than paying for it twice.

---

## Working with plans in code

```python
from loomcraft import (
    parse_plan, validate_plan, update_step, get_step, propagate_skips,
)
from loomcraft.plan import ensure_dependencies_succeeded, ensure_step_startable

validated = validate_plan(raw, current=session.current_plan(), registry=registry)
session.publish_plan(validated)

plan = parse_plan(session.current_plan())   # typed Plan object
plan.layers
plan.step("clean").status
plan.ready_steps()

state = update_step(session.current_plan(), "clean", "running")
state = update_step(state, "clean", "succeeded", summary="12 rows",
                    execution={"kind": "capability", "id": "run-abc"})
session.update_current_plan(state)
```

### Visualising

```python
from loomcraft import to_dot
print(to_dot(plan.adjacency, labels={s.id: s.title for s in plan.steps}))
```

```python
from loomcraft import critical_path
critical_path(plan.adjacency)                    # longest chain — the floor on wall-clock
critical_path(plan.adjacency, weights=measured)  # feed real durations to find the bottleneck
```

---

## Designing good plans

Guidance worth putting in your system prompt.

**Model real data dependencies, nothing else.** The most common plan-quality
problem is an agent chaining independent steps out of narrative habit and
accidentally serialising its own work.

```jsonc
// ✗ profile does not consume anything outliers produces
{"id": "outliers", "depends_on": ["clean"]},
{"id": "profile",  "depends_on": ["outliers"]}

// ✓ both read the cleaned table; they run concurrently
{"id": "outliers", "depends_on": ["clean"]},
{"id": "profile",  "depends_on": ["clean"]}
```

**Put a `review` step before `answer` on anything consequential.** It forces the
agent to read what was produced rather than assume it, and it gives a reviewer a
place to see that check happened.

**Keep steps at the granularity of a capability.** One step per unit of work.
Fewer, larger steps hide failures; more, smaller steps produce a graph nobody
can read.

**Name steps after outcomes.** `clean-table` and `detect-outliers`, not `step1`
and `step2` — the ids appear in tool calls, events, node badges, and error
messages.

**Bound the plan.** 256 steps is the hard ceiling (24 is the recommended
readable default), but a plan approaching it usually
wants a workflow for the fixed parts.

---

## Error reference

| Exception | `code` | Cause |
| --- | --- | --- |
| `PlanValidationError` | `PLAN_INVALID` | Structure, kind, registry, or revision rule violated |
| `StepTransitionError` | `STEP_TRANSITION_INVALID` | Disallowed status transition |
| `UnknownStepError` | `STEP_UNKNOWN` | The step id is not in the plan |
| `DependencyError` | `STEP_DEPENDENCIES_INCOMPLETE` | A dependency has not succeeded |

All derive from `LoomCraftError` and carry `.code` and `.public_message`, so a
host can map them onto HTTP statuses or UI copy without string matching.

Next: [Agent integration](03-agent-integration.md).
