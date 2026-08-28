.PHONY: install test lint build typecheck check docs examples

install:
	python -m pip install -e ".[dev]"
	npm ci

test:
	python -m pytest -q
	npm --prefix packages/renderer test

lint:
	python -m ruff check packages/core/src --select F,E9,B023

build:
	npm --prefix packages/renderer run build

typecheck:
	npm --prefix packages/renderer run typecheck

check: test lint typecheck build

docs:
	python tools/check_docs.py

examples:
	python examples/python/retry_parallel.py
