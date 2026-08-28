# Python examples

All examples run without a model key or third-party dependency:

```bash
python examples/python/retry_parallel.py
python examples/python/ai_planning.py
python examples/python/approval_pause.py
python examples/python/input_request.py
```

`fastapi_app.py` is an embedding sketch and is not started by the examples;
run it with your preferred ASGI server after installing the optional FastAPI
extra.

For example: `uvicorn examples.python.fastapi_app:app --reload`.

`retry_parallel.py` is the main end-to-end scenario. It publishes a validated
plan, runs two independent branches concurrently, retries a transient failure,
joins the results, registers artifacts, and prints the replayable event stream.
The invariant checks are listed in [expected-output.md](expected-output.md).
