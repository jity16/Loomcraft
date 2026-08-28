# Expected signals

The exact run id and artifact ids are random, but `retry_parallel.py` should
print these invariants:

- `Run status: succeeded`
- `Attempts: 3` for the deliberately flaky quality step
- two source `step_attempt` events before the join becomes runnable
- at least two `step_retry` events and one `artifact_registered` event
- a final `execution_finished` event with status `succeeded`

