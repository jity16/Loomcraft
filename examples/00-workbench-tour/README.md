# 00 · Workbench tour

The smallest LoomCraft example that still looks like a real plan. An uploaded
table is normalised once, then three independent branches run in parallel:

![The workbench tour: one normalisation step fans out into three concurrent branches and fans back in to a report.](../../assets/workbench-tour.svg)

```text
normalize ─┬─ pca       ─ scan.yield  ─ qc.yield  ─┐
           ├─ phenotype ─ scan.depth  ─ qc.depth  ─┼─ report
           └─ kinship   ─ scan.height ─ qc.height ─┘
```

The script first submits a cyclic graph to show that validation rejects it
before execution. The runner for `scan.depth` then deliberately returns a
retryable error once. The
`report` capability requires approval, so the engine parks before invoking its
runner and resumes only after the host resolves the gate. The script prints
the measured overlap of the three preparation steps, the retry count, the
final node statuses, and the event-log verification result.

```bash
python examples/00-workbench-tour/run.py
```

There is no model call in this tour. The plan is sent through the same
`publish_plan` and `execute_plan` broker path that `AnthropicAgent`,
`OpenAICompatibleAgent`, and app-server hosts use.
