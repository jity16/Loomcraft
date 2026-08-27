"""Example 1 — a CSV analysis toolkit registered as LoomCraft capabilities.

Everything here is ordinary Python. Nothing imports LoomCraft internals; each
runner is an async function that reads its declared inputs and emits artifacts.

The capabilities are shaped to exercise the engine's real behaviour:

* ``csv.profile`` and ``csv.outliers`` both depend only on the cleaned table, so
  a plan that lists both gets **genuine parallel execution** — no fan-out syntax.
* ``csv.fetch_reference`` is deliberately flaky and declares ``max_attempts=3``,
  which shows **retry with exponential backoff**.
* ``csv.publish`` declares ``requires_approval=True``, which parks the run in
  ``waiting_approval`` until a human resolves it.
* ``csv.clean`` accepts either a single table or a header + body pair, showing
  **input variants**.
"""

from __future__ import annotations

import csv
import io
import json
import math
import statistics
from collections import Counter
from typing import Any

from loomcraft import (
    Capability,
    CapabilityInput,
    NodeContext,
    NodeResult,
    Parameter,
    Port,
    Registry,
    Workflow,
    WorkflowNode,
)

registry = Registry()


# ── Helpers ─────────────────────────────────────────────────────────────────


def read_rows(text: str) -> tuple[list[str], list[dict[str, str]]]:
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    return list(reader.fieldnames or []), rows


def numeric(values: list[str]) -> list[float]:
    out: list[float] = []
    for value in values:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            out.append(number)
    return out


def write_csv(headers: list[str], rows: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=headers, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buffer.getvalue()


# ── 1. Clean ────────────────────────────────────────────────────────────────

CLEAN = Capability(
    id="csv.clean",
    name="Clean a CSV table",
    version="1",
    description=(
        "Drop blank rows, trim whitespace, normalise headers to snake_case, and "
        "optionally de-duplicate. Accepts either one complete table, or a header "
        "file plus a body file."
    ),
    runner="csv.clean",
    inputs=(
        CapabilityInput(
            key="table",
            name="Table",
            description="A CSV file with a header row.",
            allowed_extensions=(".csv",),
        ),
        CapabilityInput(
            key="header",
            name="Header",
            description="A one-line CSV containing only the header row.",
            allowed_extensions=(".csv",),
        ),
        CapabilityInput(
            key="body",
            name="Body",
            description="A headerless CSV containing only data rows.",
            allowed_extensions=(".csv",),
        ),
    ),
    # Exactly one of these combinations must be supplied — the broker rejects a
    # call that mixes them or supplies only half of one.
    input_variants=(("table",), ("header", "body")),
    outputs=(Port(name="cleaned", artifact_type="csv", description="The cleaned table."),),
    parameters={
        "drop_duplicates": Parameter(
            type="boolean", description="Remove duplicate rows.", default=True
        ),
        "min_row_fill": Parameter(
            type="number",
            description="Drop rows with less than this fraction of non-empty cells.",
            minimum=0,
            maximum=1,
            default=0.5,
        ),
    },
    tags=("csv", "clean", "preprocess"),
)


@registry.capability_runner(CLEAN)
async def clean(ctx: NodeContext) -> NodeResult:
    if ctx.has_input("table"):
        text = ctx.input("table").read_text()
    else:
        text = ctx.input("header").read_text().rstrip("\n") + "\n" + ctx.input("body").read_text()

    headers, rows = read_rows(text)
    if not headers:
        # Not retryable: re-running the same empty file cannot help.
        return NodeResult.fail("the table has no header row")

    normalised = [header.strip().lower().replace(" ", "_") for header in headers]
    threshold = float(ctx.parameters["min_row_fill"])
    seen: set[tuple[str, ...]] = set()
    kept: list[dict[str, str]] = []
    dropped_sparse = 0
    dropped_duplicate = 0

    for row in rows:
        values = [(row.get(header) or "").strip() for header in headers]
        filled = sum(1 for value in values if value)
        if not filled or filled / len(headers) < threshold:
            dropped_sparse += 1
            continue
        record = dict(zip(normalised, values, strict=False))
        if ctx.parameters["drop_duplicates"]:
            fingerprint = tuple(values)
            if fingerprint in seen:
                dropped_duplicate += 1
                continue
            seen.add(fingerprint)
        kept.append(record)

    ctx.progress(0.9, f"kept {len(kept)} of {len(rows)} rows")
    ctx.emit("cleaned", "cleaned.csv", write_csv(normalised, kept))
    return NodeResult.ok(
        rows_in=len(rows),
        rows_out=len(kept),
        dropped_sparse=dropped_sparse,
        dropped_duplicate=dropped_duplicate,
        columns=normalised,
    )


# ── 2. Profile (parallel branch A) ──────────────────────────────────────────

PROFILE = Capability(
    id="csv.profile",
    name="Profile columns",
    description="Per-column type inference, null counts, cardinality, and statistics.",
    runner="csv.profile",
    inputs=(
        CapabilityInput(
            key="cleaned",
            name="Cleaned table",
            description="The cleaned CSV to profile.",
            allowed_extensions=(".csv",),
        ),
    ),
    outputs=(Port(name="profile", artifact_type="json"),),
    parameters={
        "top_values": Parameter(
            type="integer",
            description="How many frequent values to report per column.",
            minimum=1,
            maximum=20,
            default=5,
        )
    },
    tags=("csv", "profile", "statistics", "describe"),
)


@registry.capability_runner(PROFILE)
async def profile(ctx: NodeContext) -> NodeResult:
    headers, rows = read_rows(ctx.input("cleaned").read_text())
    top_n = int(ctx.parameters["top_values"])
    columns: list[dict[str, Any]] = []

    for index, header in enumerate(headers):
        values = [(row.get(header) or "").strip() for row in rows]
        present = [value for value in values if value]
        numbers = numeric(present)
        column: dict[str, Any] = {
            "name": header,
            "non_null": len(present),
            "null": len(values) - len(present),
            "distinct": len(set(present)),
            "kind": "numeric" if numbers and len(numbers) >= len(present) * 0.9 else "text",
        }
        if column["kind"] == "numeric" and numbers:
            column["min"] = min(numbers)
            column["max"] = max(numbers)
            column["mean"] = round(statistics.fmean(numbers), 6)
            if len(numbers) > 1:
                column["stdev"] = round(statistics.stdev(numbers), 6)
        else:
            column["top_values"] = [
                {"value": value, "count": count}
                for value, count in Counter(present).most_common(top_n)
            ]
        columns.append(column)
        ctx.progress((index + 1) / max(1, len(headers)), f"profiled {header}")

    report = {"row_count": len(rows), "column_count": len(headers), "columns": columns}
    ctx.emit("profile", "profile.json", json.dumps(report, indent=2))
    return NodeResult.ok(row_count=len(rows), column_count=len(headers))


# ── 3. Outliers (parallel branch B) ─────────────────────────────────────────

OUTLIERS = Capability(
    id="csv.outliers",
    name="Detect outliers",
    description="Flag numeric values beyond a z-score threshold, per column.",
    runner="csv.outliers",
    inputs=(
        CapabilityInput(
            key="cleaned",
            name="Cleaned table",
            description="The cleaned CSV to scan.",
            allowed_extensions=(".csv",),
        ),
    ),
    outputs=(Port(name="outliers", artifact_type="json"),),
    parameters={
        "z_threshold": Parameter(
            type="number",
            description="Absolute z-score above which a value is an outlier.",
            minimum=1,
            maximum=10,
            default=3.0,
        )
    },
    tags=("csv", "outliers", "anomaly", "quality"),
)


@registry.capability_runner(OUTLIERS)
async def outliers(ctx: NodeContext) -> NodeResult:
    headers, rows = read_rows(ctx.input("cleaned").read_text())
    threshold = float(ctx.parameters["z_threshold"])
    findings: list[dict[str, Any]] = []

    for header in headers:
        numbers = numeric([(row.get(header) or "") for row in rows])
        if len(numbers) < 3:
            continue
        mean = statistics.fmean(numbers)
        spread = statistics.stdev(numbers)
        if spread == 0:
            continue
        for position, row in enumerate(rows):
            raw = (row.get(header) or "").strip()
            parsed = numeric([raw])
            if not parsed:
                continue
            score = abs(parsed[0] - mean) / spread
            if score >= threshold:
                findings.append(
                    {
                        "row": position,
                        "column": header,
                        "value": parsed[0],
                        "z_score": round(score, 3),
                    }
                )

    ctx.emit(
        "outliers",
        "outliers.json",
        json.dumps({"threshold": threshold, "findings": findings}, indent=2),
    )
    return NodeResult.ok(finding_count=len(findings))


# ── 4. Reference lookup (retry demo) ────────────────────────────────────────

_ATTEMPTS: dict[str, int] = {}

REFERENCE = Capability(
    id="csv.fetch_reference",
    name="Fetch reference thresholds",
    description=(
        "Look up domain reference thresholds from an external service. The "
        "service is rate-limited, so this capability retries with backoff."
    ),
    runner="csv.fetch_reference",
    outputs=(Port(name="reference", artifact_type="json"),),
    # The engine will re-run this up to three times, waiting 0.2s then 0.4s.
    max_attempts=3,
    retry_backoff_seconds=0.2,
    timeout_seconds=10,
    parameters={
        "dataset": Parameter(
            type="string", description="Reference dataset name.", default="default"
        )
    },
    tags=("reference", "lookup", "thresholds"),
)


@registry.capability_runner(REFERENCE)
async def fetch_reference(ctx: NodeContext) -> NodeResult:
    """Simulates a flaky upstream: the first two attempts fail transiently."""
    key = ctx.parameters["dataset"]
    _ATTEMPTS[key] = _ATTEMPTS.get(key, 0) + 1
    if _ATTEMPTS[key] < 3:
        ctx.log(f"reference service unavailable (attempt {ctx.attempt})", "warn")
        # `retry` marks this as worth another attempt; a plain `fail` would not be.
        return NodeResult.retry(f"reference service returned 503 (attempt {ctx.attempt})")

    payload = {"dataset": key, "max_null_fraction": 0.2, "max_outlier_rate": 0.05}
    ctx.emit("reference", "reference.json", json.dumps(payload, indent=2))
    return NodeResult.ok(attempts_used=ctx.attempt)


# ── 5. Report (fan-in) ──────────────────────────────────────────────────────

REPORT = Capability(
    id="csv.report",
    name="Compose a quality report",
    description="Combine the profile, outlier scan, and reference thresholds into Markdown.",
    runner="csv.report",
    inputs=(
        CapabilityInput(key="profile", name="Profile", description="Profile JSON.", allowed_extensions=(".json",)),
        CapabilityInput(key="outliers", name="Outliers", description="Outlier JSON.", allowed_extensions=(".json",)),
        CapabilityInput(key="reference", name="Reference", description="Reference JSON.", allowed_extensions=(".json",)),
    ),
    # Only the first two are required; the reference file is optional context.
    input_variants=(("profile", "outliers"),),
    outputs=(Port(name="report", artifact_type="md"),),
    tags=("report", "markdown", "summary"),
)


@registry.capability_runner(REPORT)
async def report(ctx: NodeContext) -> NodeResult:
    profile_data = json.loads(ctx.input("profile").read_text())
    outlier_data = json.loads(ctx.input("outliers").read_text())
    reference_data = (
        json.loads(ctx.input("reference").read_text()) if ctx.has_input("reference") else {}
    )

    rows = profile_data["row_count"]
    lines = [
        "# Data quality report",
        "",
        f"- Rows: **{rows}**",
        f"- Columns: **{profile_data['column_count']}**",
        f"- Outliers flagged: **{len(outlier_data['findings'])}** "
        f"(z ≥ {outlier_data['threshold']})",
        "",
        "## Columns",
        "",
        "| Column | Kind | Non-null | Distinct | Notes |",
        "| --- | --- | ---: | ---: | --- |",
    ]

    max_null_fraction = float(reference_data.get("max_null_fraction", 1.0))
    concerns: list[str] = []
    for column in profile_data["columns"]:
        null_fraction = column["null"] / rows if rows else 0
        note = ""
        if null_fraction > max_null_fraction:
            note = f"⚠ {null_fraction:.0%} null (threshold {max_null_fraction:.0%})"
            concerns.append(column["name"])
        elif column["kind"] == "numeric" and "mean" in column:
            note = f"mean {column['mean']:g}"
        lines.append(
            f"| `{column['name']}` | {column['kind']} | {column['non_null']} | "
            f"{column['distinct']} | {note} |"
        )

    if outlier_data["findings"]:
        lines += ["", "## Outliers", ""]
        for finding in outlier_data["findings"][:20]:
            lines.append(
                f"- row {finding['row']}, `{finding['column']}` = {finding['value']} "
                f"(z = {finding['z_score']})"
            )

    lines += ["", "## Verdict", ""]
    lines.append(
        f"⚠ Columns needing attention: {', '.join(concerns)}."
        if concerns
        else "✅ No column exceeded the reference null threshold."
    )

    ctx.emit("report", "quality-report.md", "\n".join(lines) + "\n")
    return NodeResult.ok(concern_count=len(concerns))


# ── 6. Publish (human approval demo) ────────────────────────────────────────

PUBLISH = Capability(
    id="csv.publish",
    name="Publish the report",
    description=(
        "Publish the finished report to the shared reporting space. Outward-"
        "facing and hard to reverse, so it requires human approval first."
    ),
    runner="csv.publish",
    inputs=(
        CapabilityInput(
            key="report",
            name="Report",
            description="The Markdown report to publish.",
            allowed_extensions=(".md",),
        ),
    ),
    outputs=(Port(name="receipt", artifact_type="json"),),
    requires_approval=True,
    tags=("publish", "share", "deliver"),
)


@registry.capability_runner(PUBLISH)
async def publish(ctx: NodeContext) -> NodeResult:
    # The engine parks the node here. Only after `run.approve(node_id, True)`
    # does it count as succeeded — and note the side effect is *not* performed
    # before the gate, which is the point of returning early.
    if ctx.attempt == 1 and not ctx.config.get("approved"):
        return NodeResult.needs_approval(
            f"about to publish {ctx.input('report').filename} to the shared space"
        )
    ctx.emit("receipt", "receipt.json", json.dumps({"published": True}, indent=2))
    return NodeResult.ok()


# ── A registered workflow (fixed SOP) ───────────────────────────────────────

QUALITY_SOP = Workflow(
    id="csv.quality_sop",
    name="Standard CSV quality review",
    description=(
        "The fixed clean → (profile ‖ outliers) → report pipeline, offered as a "
        "single unit for users who want the standard review with no deviation."
    ),
    inputs=(
        CapabilityInput(
            key="table",
            name="Table",
            description="The CSV to review.",
            allowed_extensions=(".csv",),
        ),
    ),
    nodes=(
        WorkflowNode(
            id="clean",
            name="Clean",
            runner="csv.clean",
            inputs=("table",),
            outputs=(Port(name="cleaned", artifact_type="csv"),),
        ),
        # These two declare the same single dependency, so the engine runs them
        # concurrently — the workflow author does not opt in to parallelism.
        WorkflowNode(
            id="profile",
            name="Profile",
            runner="csv.profile",
            depends_on=("clean",),
            outputs=(Port(name="profile", artifact_type="json"),),
        ),
        WorkflowNode(
            id="outliers",
            name="Outliers",
            runner="csv.outliers",
            depends_on=("clean",),
            outputs=(Port(name="outliers", artifact_type="json"),),
        ),
        WorkflowNode(
            id="report",
            name="Report",
            runner="csv.report",
            depends_on=("profile", "outliers"),
            outputs=(Port(name="report", artifact_type="md"),),
        ),
    ),
    parameters={
        "drop_duplicates": Parameter(
            type="boolean", description="Remove duplicate rows.", default=True
        ),
        "min_row_fill": Parameter(
            type="number", description="Row fill threshold.", minimum=0, maximum=1, default=0.5
        ),
        "top_values": Parameter(
            type="integer", description="Frequent values per column.", minimum=1, maximum=20, default=5
        ),
        "z_threshold": Parameter(
            type="number", description="Outlier z-score.", minimum=1, maximum=10, default=3.0
        ),
    },
    tags=("csv", "sop", "quality"),
)

registry.register_workflow(QUALITY_SOP)


def reset_reference_attempts() -> None:
    """Test helper: make the flaky reference service fail again."""
    _ATTEMPTS.clear()


assert not registry.validate(), registry.validate()
