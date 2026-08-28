# Contributing

## Getting set up

```bash
make install     # editable core install + renderer dependencies
make check       # what CI runs: lint, tests, typecheck, build, docs
```

Everything runs offline. The examples use `ScriptedAgent`, so no API key is
needed to exercise the full plan → execute → render path.

## What belongs in this repository

LoomCraft is a library. The public surface is the Python package, the renderer
package, `docs/`, and the runnable examples.

**Do not commit development notes.** Design scratch, migration plans, review
logs, agent transcripts and TODO files are working artifacts, not documentation.
`.gitignore` excludes the usual names; if you need somewhere to keep them, use
`notes/` locally. Anything a user would not read to *use* LoomCraft does not
belong in the repository.

If a note turns out to contain something users need, move that content into the
relevant `docs/` page in the voice of the rest of the documentation, and drop
the note.

## Changing the contract

The plan, event and tool schemas in `packages/core/schema/` are generated:

```bash
make schema      # regenerate after changing tools.py or plan.py
```

CI fails if they are stale. Contract changes need a `CHANGELOG.md` entry.

## Tests

- `packages/core/tests/` — engine, broker, plan and hardening tests.
- `packages/renderer/tests/` — reducer and layout tests, run by `node --test`.

A bug fix should come with a test that fails without it. `test_hardening.py` is
organised around what a defect would have *cost* — an unauthorised side effect,
a deliverable nobody produced, host detail nobody should see — and new
regressions should follow that framing rather than being named after the patch.

## Style

Match the surrounding code. Two things the codebase is consistent about:

- **Comments explain why, not what.** If a line needs a comment to say what it
  does, rename something instead.
- **Errors returned to a model never echo the rejected input.** A model handed
  its own bad payload back tends to send it again, and the payload may quote
  user data.
