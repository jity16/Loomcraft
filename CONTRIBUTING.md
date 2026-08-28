# Contributing

Keep the core independent of domain applications and optional frameworks.
Changes that affect the JSON contract should update `packages/core/schema`, its
`core/schema` compatibility mirror, the public docs, and a replay/compatibility
test. Run:

```bash
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check packages/core/src --select F,E9,B023
npm run check
python tools/check_docs.py
```

Handlers must be deterministic under retry or document their idempotency
requirements. Do not add secrets, absolute host paths, or business-specific
registries to the core package.
