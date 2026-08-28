# 00 · Workbench tour

The smallest LoomCraft example that still looks like a real plan. An uploaded
table is normalised once, three preparation steps run in parallel, their
evidence is assembled, and three analysis branches run in parallel again:

The complete graph has 13 nodes: 1 normalisation, 3 preparations, 1 assembly,
3 analyses, 3 checks, 1 review, and 1 report.

![The workbench tour: preparation fans in to one model context, then fans out into three concurrent analysis lanes, three quality checks, a review, and a report.](../../assets/workbench-tour.svg)

```text
    normalize ─┬─ pca       ───────────┐
               ├─ phenotype ───────────┼─ assemble ─┬─ scan.yield  ── qc.yield  ─┐
               └─ kinship   ───────────┘             ├─ scan.depth  ── qc.depth  ──┼─ review ── report
                                                     └─ scan.height ── qc.height ─┘
```

The script first submits a cyclic graph to show that validation rejects it
before execution. The runner for `scan.depth` then deliberately returns a
retryable error once. The `report` capability requires approval, so the engine parks before invoking its
runner and resumes only after the host resolves the gate. The script prints
the measured overlap of the three preparation steps, the retry count, the
final node statuses, and the event-log verification result.

```bash
python examples/00-workbench-tour/run.py
```

There is no model call in this tour. The plan is sent through the same
`publish_plan` and `execute_plan` broker path that `AnthropicAgent`,
`OpenAICompatibleAgent`, and app-server hosts use.
