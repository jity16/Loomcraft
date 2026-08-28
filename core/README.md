# Source compatibility shim

The publishable Python package lives in
`packages/core/src/loomcraft`. This directory only keeps the historical
`PYTHONPATH=core` import path working; it contains no second implementation.

Install the canonical package with `pip install -e packages/core` or
`pip install -e .` from the repository root.
