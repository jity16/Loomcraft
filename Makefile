.PHONY: install test lint typecheck build schema docs check examples clean

PYTHON ?= python
RENDERER = --prefix packages/renderer

install:
	$(PYTHON) -m pip install -e "packages/core[dev]"
	npm ci $(RENDERER)

test:
	$(PYTHON) -m pytest -q packages/core/tests
	npm test $(RENDERER)

lint:
	$(PYTHON) -m ruff check packages/core/src --select F,E9,B023

typecheck:
	npm run typecheck $(RENDERER)

build:
	npm run build $(RENDERER)

schema:
	$(PYTHON) tools/export_schema.py

docs:
	$(PYTHON) tools/export_schema.py --check
	$(PYTHON) tools/check_docs.py

# What CI runs. Use this before opening a pull request.
check: lint test typecheck build docs

examples:
	$(PYTHON) examples/00-workbench-tour/run.py
	$(PYTHON) examples/01-gwas-discovery/run_scripted.py
	$(PYTHON) examples/02-literature-meta/run_scripted.py
	$(PYTHON) examples/03-objectives-and-scheduling/run.py

clean:
	rm -rf packages/renderer/dist .pytest_cache .ruff_cache
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
