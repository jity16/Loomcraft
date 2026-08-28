"""Legacy installer shim for environments with pre-PEP-660 pip."""

from setuptools import find_packages, setup


setup(
    name="loomcraft",
    version="0.1.0",
    description="An AI-native DAG planning and execution engine with provider-neutral tools and live rendering",
    package_dir={"": "packages/core/src"},
    packages=find_packages("packages/core/src"),
    package_data={"loomcraft": ["py.typed", "schemas/*.json", "schemas/README.md"]},
    data_files=[("share/loomcraft/schema", ["packages/core/schema/plan.schema.json", "packages/core/schema/event.schema.json", "packages/core/schema/tools.schema.json"])],
    python_requires=">=3.11",
)
