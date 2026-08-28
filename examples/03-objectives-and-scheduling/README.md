# Example 3 · Objectives, whole-plan scheduling, and a Codex-style host

```bash
python examples/03-objectives-and-scheduling/run.py
```

No model, no API key, no network. Every call goes through the real broker,
engine and event log.

A breeder asks two questions about one field trial:

1. Which genotypes have the highest yield effect?
2. Is there a maternal component to yield?

The first is answerable from the data. The second is not — the trial table has
no dam column, so the maternal variance is not identifiable no matter how the
analysis is arranged. **What the system does about the second question is the
example.**

## What you will see

| Section | What it demonstrates |
| --- | --- |
| 1 | Coverage marked `executed` is refused when it cites no step or artifact |
| 2 | Objectives declared before the work; `scan`/`spread`/`maternal` land in one layer |
| 3 | `execute_plan` — six nodes, one run, genuine concurrency, retry driven from the plan |
| 4 | `on_failure: "continue"` — the maternal branch fails, the run still succeeds, the failure stays visible |
| 5 | A `review` step bound to `review.calibration`, which the agent then cannot self-report |
| 6 | A revision may reclassify `q2` as `not_estimable`, but not drop it |
| 7 | The same broker driven over JSON-RPC the way a Codex app-server drives it |
| 8 | The hash-chained audit trail over the whole thing |

## The point

Two things make this different from a pipeline that happened to produce output.

**The unanswerable question is still in the record.** Nothing forced the agent to
keep asking about maternal effects once that branch failed — except that the
ledger will not let a revision drop a declared objective. The final plan says
`not_estimable`, gives the reason, and names what would change it: a pedigree
export with dam ids. That is a more useful result than silence.

**The failure did not cascade.** `maternal` is marked `on_failure: "continue"`,
so an exploratory branch coming back empty did not skip the annotation and
review steps downstream of a *different* branch. The run reports `succeeded`
with `maternal` listed in `failed_nodes` as `tolerated`.

Neither behaviour is narrated by the example. Both are enforced by the server,
and you can see the refusals in sections 1, 5 and 6.

## Files

| File | What's in it |
| --- | --- |
| `capabilities.py` | Six capabilities, each exercising one scheduler behaviour |
| `run.py` | The scripted run, printing what the server accepted and refused |
