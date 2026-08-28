"""Example 1 — a genome-wide association toolkit registered as LoomCraft capabilities.

Everything here is ordinary Python with no scientific dependencies: genotypes are
a small text matrix, the association test is a closed-form linear regression, and
the kinship matrix is computed by hand. That is deliberate — the point of the
example is the *shape* of the analysis, not the numerics, and a reader should be
able to follow every line without installing PLINK.

The science is real even though the scale is small. In particular the discovery
arc the example walks through is the textbook one:

  A naive association scan on a structured population produces a genomic
  inflation factor (lambda) well above 1. The p-values are not evidence of
  many true signals; they are evidence that ancestry is confounded with
  phenotype. Correcting for kinship collapses the inflation and leaves the
  hits that were real.

The capabilities are shaped so the engine's behaviour is exercised for real,
not narrated:

* ``gwas.pca`` and ``gwas.kinship`` both depend only on the QC'd genotypes, so a
  plan that lists both gets **genuine parallel execution** — there is no fan-out
  syntax to write.
* ``gwas.annotate`` calls a flaky external gene server and declares
  ``max_attempts=3``, which exercises **retry with exponential backoff**.
* ``gwas.register_finding`` declares ``requires_approval=True``: registering a
  locus as a claimed discovery is outward-facing and hard to retract, so the
  engine parks the node *before invoking the runner* and waits for a human. The
  runner therefore contains no approval check of its own.
* ``gwas.qc`` accepts either a PLINK-style ``.bed`` + ``.bim`` + ``.fam`` triple
  or a single ``.vcf``, which is what **input variants** are for — a real pair or
  a real VCF, never half of each.
* ``gwas.associate`` genuinely fails when asked for a mixed model without a
  kinship matrix, which is what makes the replan in ``run_scripted.py`` a real
  path rather than a scripted one.
"""

from __future__ import annotations

import json
import math
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


# ── The data model ──────────────────────────────────────────────────────────
#
# A cohort is a tiny tab-separated matrix:
#
#   sample  ancestry  phenotype  rs1  rs2  rs3 ...
#   S001    north     41.2       0    1    2
#
# Genotypes are allele dosages in {0, 1, 2}, missing is ".". That is exactly the
# information a .bed/.bim/.fam triple carries, minus the binary packing.


def read_cohort(text: str) -> dict[str, Any]:
    lines = [line for line in text.splitlines() if line.strip()]
    header = lines[0].split("\t")
    markers = header[3:]
    samples: list[dict[str, Any]] = []
    for line in lines[1:]:
        cells = line.split("\t")
        samples.append(
            {
                "sample": cells[0],
                "ancestry": cells[1],
                "phenotype": float(cells[2]),
                "genotypes": cells[3:],
            }
        )
    return {"markers": markers, "samples": samples}


def write_cohort(cohort: dict[str, Any]) -> str:
    lines = ["\t".join(["sample", "ancestry", "phenotype", *cohort["markers"]])]
    for sample in cohort["samples"]:
        lines.append(
            "\t".join(
                [
                    sample["sample"],
                    sample["ancestry"],
                    f"{sample['phenotype']:.4f}",
                    *sample["genotypes"],
                ]
            )
        )
    return "\n".join(lines) + "\n"


def dosages(cohort: dict[str, Any], index: int) -> list[float | None]:
    out: list[float | None] = []
    for sample in cohort["samples"]:
        raw = sample["genotypes"][index]
        out.append(None if raw == "." else float(raw))
    return out


def regress(x: list[float], y: list[float]) -> tuple[float, float, float]:
    """Ordinary least squares of y on x. Returns (beta, standard error, p)."""
    n = len(x)
    mean_x = sum(x) / n
    mean_y = sum(y) / n
    sxx = sum((value - mean_x) ** 2 for value in x)
    if sxx == 0:
        return 0.0, float("inf"), 1.0
    sxy = sum((x[i] - mean_x) * (y[i] - mean_y) for i in range(n))
    beta = sxy / sxx
    intercept = mean_y - beta * mean_x
    residuals = [y[i] - (intercept + beta * x[i]) for i in range(n)]
    dof = n - 2
    if dof <= 0:
        return beta, float("inf"), 1.0
    sigma2 = sum(value**2 for value in residuals) / dof
    stderr = math.sqrt(sigma2 / sxx) if sigma2 > 0 else 1e-12
    t = beta / stderr if stderr else 0.0
    return beta, stderr, two_sided_p(abs(t), dof)


def two_sided_p(t: float, dof: int) -> float:
    """Student-t survival function, doubled. Closed form, no SciPy."""
    if t <= 0:
        return 1.0
    x = dof / (dof + t * t)
    return max(min(betainc_half(dof / 2.0, 0.5, x), 1.0), 1e-300)


def betainc_half(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta I_x(a, b), via the standard continued fraction."""
    if x <= 0:
        return 0.0
    if x >= 1:
        return 1.0
    front = math.exp(
        a * math.log(x) + b * math.log(1 - x) + math.lgamma(a + b)
        - math.lgamma(a) - math.lgamma(b)
    ) / a
    # Lentz's algorithm.
    f, c, d = 1.0, 1.0, 0.0
    for i in range(0, 220):
        m = i // 2
        if i == 0:
            numerator = 1.0
        elif i % 2 == 0:
            numerator = (m * (b - m) * x) / ((a + 2 * m - 1) * (a + 2 * m))
        else:
            numerator = -((a + m) * (a + b + m) * x) / ((a + 2 * m) * (a + 2 * m + 1))
        d = 1.0 + numerator * d
        if abs(d) < 1e-30:
            d = 1e-30
        d = 1.0 / d
        c = 1.0 + numerator / c
        if abs(c) < 1e-30:
            c = 1e-30
        f *= c * d
        if abs(1.0 - c * d) < 1e-12:
            break
    return front * (f - 1.0)


def chi2_median_from_p(p_values: list[float]) -> float:
    """The genomic inflation factor: median chi-square over its null expectation.

    lambda ~= 1 means the test statistics behave the way the null says they
    should. lambda well above 1 means something systematic — population
    structure, cryptic relatedness, batch — is inflating every statistic at
    once, and the small p-values are not the discoveries they look like.
    """
    if not p_values:
        return 1.0
    stats = sorted(inverse_chi2_1df(p) for p in p_values)
    middle = len(stats) // 2
    median = (
        stats[middle]
        if len(stats) % 2
        else (stats[middle - 1] + stats[middle]) / 2
    )
    return median / 0.4549364  # median of a 1-df chi-square


def inverse_chi2_1df(p: float) -> float:
    """chi-square with 1 df is z^2, so this is just the squared normal quantile."""
    return normal_quantile(1.0 - p / 2.0) ** 2


def normal_quantile(p: float) -> float:
    """Acklam's rational approximation to the inverse normal CDF."""
    p = min(max(p, 1e-15), 1 - 1e-15)
    a = [-3.969683028665376e01, 2.209460984245205e02, -2.759285104469687e02,
         1.383577518672690e02, -3.066479806614716e01, 2.506628277459239e00]
    b = [-5.447609879822406e01, 1.615858368580409e02, -1.556989798598866e02,
         6.680131188771972e01, -1.328068155288572e01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e00,
         -2.549732539343734e00, 4.374664141464968e00, 2.938163982698783e00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e00,
         3.754408661907416e00]
    low, high = 0.02425, 1 - 0.02425
    if p < low:
        q = math.sqrt(-2 * math.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
               ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > high:
        q = math.sqrt(-2 * math.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / \
                ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5]) * q / \
           (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


# ── 1. Quality control ──────────────────────────────────────────────────────

QC = Capability(
    id="gwas.qc",
    name="Genotype quality control",
    version="1",
    description=(
        "Filter markers on minor-allele frequency and call rate, drop samples "
        "below a call-rate floor, and mean-impute the remaining missing calls. "
        "Accepts either a PLINK .bed/.bim/.fam triple, or a single VCF."
    ),
    runner="gwas.qc",
    inputs=(
        CapabilityInput(
            key="bed",
            name="PLINK genotypes",
            description="Allele dosage matrix (.bed).",
            allowed_extensions=(".bed",),
        ),
        CapabilityInput(
            key="bim",
            name="PLINK marker map",
            description="Marker positions and alleles (.bim).",
            allowed_extensions=(".bim",),
        ),
        CapabilityInput(
            key="fam",
            name="PLINK sample file",
            description="Sample ids and phenotypes (.fam).",
            allowed_extensions=(".fam",),
        ),
        CapabilityInput(
            key="vcf",
            name="VCF",
            description="A single variant call file carrying the same information.",
            allowed_extensions=(".vcf",),
        ),
    ),
    # A real PLINK triple or a real VCF. The broker rejects a call that supplies
    # .bed without .bim, or mixes a VCF into the triple.
    input_variants=(("bed", "bim", "fam"), ("vcf",)),
    outputs=(
        Port(name="cohort", artifact_type="tsv", description="QC'd genotype matrix."),
        Port(name="qc_report", artifact_type="json", description="What was dropped, and why."),
    ),
    parameters={
        "min_maf": Parameter(
            type="number",
            description="Drop markers below this minor-allele frequency.",
            minimum=0,
            maximum=0.5,
            default=0.05,
        ),
        "min_call_rate": Parameter(
            type="number",
            description="Drop markers and samples called in less than this fraction.",
            minimum=0,
            maximum=1,
            default=0.9,
        ),
    },
    tags=("gwas", "genotype", "qc", "filter", "plink", "vcf"),
)


@registry.capability_runner(QC)
async def qc(ctx: NodeContext) -> NodeResult:
    if ctx.has_input("vcf"):
        cohort = read_cohort(ctx.input("vcf").read_text())
        source = "vcf"
    else:
        # The .bed carries the matrix; .bim and .fam are read for their counts so
        # a mismatched triple is caught here rather than three steps later.
        cohort = read_cohort(ctx.input("bed").read_text())
        declared_markers = len(
            [line for line in ctx.input("bim").read_text().splitlines() if line.strip()]
        )
        declared_samples = len(
            [line for line in ctx.input("fam").read_text().splitlines() if line.strip()]
        )
        if declared_markers != len(cohort["markers"]):
            return NodeResult.fail(
                f".bim declares {declared_markers} markers but .bed carries "
                f"{len(cohort['markers'])}"
            )
        if declared_samples != len(cohort["samples"]):
            return NodeResult.fail(
                f".fam declares {declared_samples} samples but .bed carries "
                f"{len(cohort['samples'])}"
            )
        source = "plink"

    min_maf = float(ctx.parameters["min_maf"])
    min_call = float(ctx.parameters["min_call_rate"])
    total_samples = len(cohort["samples"])

    kept_markers: list[str] = []
    kept_columns: list[int] = []
    dropped: list[dict[str, Any]] = []

    for index, marker in enumerate(cohort["markers"]):
        column = dosages(cohort, index)
        called = [value for value in column if value is not None]
        call_rate = len(called) / total_samples if total_samples else 0.0
        if call_rate < min_call:
            dropped.append({"marker": marker, "reason": "call_rate", "value": round(call_rate, 4)})
            continue
        maf = (sum(called) / (2 * len(called))) if called else 0.0
        maf = min(maf, 1 - maf)
        if maf < min_maf:
            dropped.append({"marker": marker, "reason": "maf", "value": round(maf, 4)})
            continue
        kept_markers.append(marker)
        kept_columns.append(index)
        ctx.progress((index + 1) / len(cohort["markers"]), f"screened {marker}")

    if not kept_markers:
        # Not retryable: the same file will fail the same filters every time.
        return NodeResult.fail(
            f"no marker survived MAF >= {min_maf} and call rate >= {min_call}"
        )

    # Mean-impute what is left, so downstream steps never see a missing call.
    column_means: list[float] = []
    for index in kept_columns:
        called = [value for value in dosages(cohort, index) if value is not None]
        column_means.append(sum(called) / len(called) if called else 0.0)

    kept_samples: list[dict[str, Any]] = []
    dropped_samples = 0
    for sample in cohort["samples"]:
        raw = [sample["genotypes"][index] for index in kept_columns]
        called = sum(1 for value in raw if value != ".")
        if called / len(raw) < min_call:
            dropped_samples += 1
            continue
        imputed = [
            raw[position] if raw[position] != "." else f"{column_means[position]:.3f}"
            for position in range(len(raw))
        ]
        kept_samples.append({**sample, "genotypes": imputed})

    cleaned = {"markers": kept_markers, "samples": kept_samples}
    ctx.emit("cohort", "cohort.qc.tsv", write_cohort(cleaned))
    ctx.emit(
        "qc_report",
        "qc-report.json",
        json.dumps(
            {
                "source": source,
                "markers_in": len(cohort["markers"]),
                "markers_out": len(kept_markers),
                "samples_in": total_samples,
                "samples_out": len(kept_samples),
                "dropped_markers": dropped,
                "dropped_samples": dropped_samples,
            },
            indent=2,
        ),
    )
    return NodeResult.ok(
        source=source,
        markers_out=len(kept_markers),
        samples_out=len(kept_samples),
        markers_dropped=len(dropped),
    )


# ── 2. Population structure, branch A ───────────────────────────────────────

PCA = Capability(
    id="gwas.pca",
    name="Principal components of ancestry",
    description=(
        "Project samples onto the leading axes of genotype variation. The top "
        "components are the usual proxy for continental or sub-population "
        "ancestry."
    ),
    runner="gwas.pca",
    inputs=(
        CapabilityInput(
            key="cohort",
            name="QC'd cohort",
            description="The genotype matrix to decompose.",
            allowed_extensions=(".tsv",),
        ),
    ),
    outputs=(Port(name="components", artifact_type="json"),),
    parameters={
        "components": Parameter(
            type="integer",
            description="How many principal components to retain.",
            minimum=1,
            maximum=10,
            default=2,
        )
    },
    tags=("gwas", "pca", "ancestry", "population-structure"),
)


@registry.capability_runner(PCA)
async def pca(ctx: NodeContext) -> NodeResult:
    cohort = read_cohort(ctx.input("cohort").read_text())
    wanted = int(ctx.parameters["components"])
    matrix = centred_matrix(cohort)
    components = power_iteration(matrix, wanted)

    scores = [
        {
            "sample": cohort["samples"][row]["sample"],
            "ancestry": cohort["samples"][row]["ancestry"],
            "pc": [round(project(matrix[row], axis), 5) for axis in components],
        }
        for row in range(len(matrix))
    ]

    # How much of PC1 is explained by the (known, in this toy cohort) ancestry
    # label. In a real study you would not have this column — it is here so the
    # example can state plainly what the confounder is.
    labels = sorted({row["ancestry"] for row in scores})
    separation = 0.0
    if len(labels) == 2:
        groups = [[row["pc"][0] for row in scores if row["ancestry"] == label] for label in labels]
        if all(groups):
            spread = max(
                1e-9,
                math.sqrt(sum(variance(group) for group in groups) / len(groups)),
            )
            separation = abs(mean(groups[0]) - mean(groups[1])) / spread

    ctx.emit(
        "components",
        "pca.json",
        json.dumps(
            {"components": wanted, "scores": scores, "pc1_ancestry_separation": round(separation, 3)},
            indent=2,
        ),
    )
    return NodeResult.ok(components=wanted, pc1_ancestry_separation=round(separation, 3))


def mean(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def variance(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mu = mean(values)
    return sum((value - mu) ** 2 for value in values) / (len(values) - 1)


def centred_matrix(cohort: dict[str, Any]) -> list[list[float]]:
    rows = len(cohort["samples"])
    columns = len(cohort["markers"])
    matrix = [[float(cohort["samples"][r]["genotypes"][c]) for c in range(columns)] for r in range(rows)]
    for c in range(columns):
        mu = mean([matrix[r][c] for r in range(rows)])
        for r in range(rows):
            matrix[r][c] -= mu
    return matrix


def project(row: list[float], axis: list[float]) -> float:
    return sum(row[i] * axis[i] for i in range(len(axis)))


def power_iteration(matrix: list[list[float]], count: int) -> list[list[float]]:
    """Leading eigenvectors of the marker covariance, by deflated power iteration."""
    columns = len(matrix[0]) if matrix else 0
    found: list[list[float]] = []
    for index in range(count):
        # A fixed, index-dependent start vector — deterministic, so two runs of
        # the same plan produce the same picture.
        vector = [math.sin(index + 1 + i * 0.7) for i in range(columns)]
        for _ in range(80):
            product = [0.0] * columns
            for row in matrix:
                weight = sum(row[i] * vector[i] for i in range(columns))
                for i in range(columns):
                    product[i] += weight * row[i]
            for previous in found:  # deflate against what we already have
                overlap = sum(product[i] * previous[i] for i in range(columns))
                for i in range(columns):
                    product[i] -= overlap * previous[i]
            norm = math.sqrt(sum(value**2 for value in product))
            if norm < 1e-12:
                break
            vector = [value / norm for value in product]
        found.append(vector)
    return found


# ── 3. Population structure, branch B ───────────────────────────────────────

KINSHIP = Capability(
    id="gwas.kinship",
    name="Genomic relatedness matrix",
    description=(
        "Compute the realised kinship (GRM) between every pair of samples. This "
        "is what a mixed-model association uses to absorb both ancestry and "
        "cryptic relatedness as a random effect."
    ),
    runner="gwas.kinship",
    inputs=(
        CapabilityInput(
            key="cohort",
            name="QC'd cohort",
            description="The genotype matrix.",
            allowed_extensions=(".tsv",),
        ),
    ),
    outputs=(Port(name="grm", artifact_type="json"),),
    tags=("gwas", "kinship", "grm", "relatedness", "mixed-model"),
)


@registry.capability_runner(KINSHIP)
async def kinship(ctx: NodeContext) -> NodeResult:
    cohort = read_cohort(ctx.input("cohort").read_text())
    rows = len(cohort["samples"])
    columns = len(cohort["markers"])

    # Standardise each marker, then GRM = ZZ'/m.
    standardised = [[0.0] * columns for _ in range(rows)]
    for c in range(columns):
        column = [float(cohort["samples"][r]["genotypes"][c]) for r in range(rows)]
        mu = mean(column)
        sd = math.sqrt(variance(column)) or 1.0
        for r in range(rows):
            standardised[r][c] = (column[r] - mu) / sd

    grm = [
        [
            round(sum(standardised[a][c] * standardised[b][c] for c in range(columns)) / columns, 5)
            for b in range(rows)
        ]
        for a in range(rows)
    ]
    ctx.progress(0.9, f"{rows}x{rows} relatedness matrix")

    off_diagonal = [grm[a][b] for a in range(rows) for b in range(rows) if a != b]
    ctx.emit(
        "grm",
        "kinship.json",
        json.dumps(
            {
                "samples": [sample["sample"] for sample in cohort["samples"]],
                "matrix": grm,
                "max_offdiagonal": round(max(off_diagonal), 4) if off_diagonal else 0.0,
            },
            indent=2,
        ),
    )
    return NodeResult.ok(
        samples=rows,
        max_offdiagonal=round(max(off_diagonal), 4) if off_diagonal else 0.0,
    )


# ── 4. The association scan ─────────────────────────────────────────────────

ASSOCIATE = Capability(
    id="gwas.associate",
    name="Association scan",
    description=(
        "Test every marker for association with the phenotype. In 'linear' mode "
        "this is a plain per-marker regression. In 'mlm' mode the leading "
        "eigenvectors of the supplied kinship matrix are fitted as covariates "
        "first, which absorbs population structure — 'mlm' therefore requires a "
        "kinship matrix and fails without one."
    ),
    runner="gwas.associate",
    inputs=(
        CapabilityInput(
            key="cohort",
            name="QC'd cohort",
            description="The genotype matrix to scan.",
            allowed_extensions=(".tsv",),
        ),
        CapabilityInput(
            key="grm",
            name="Kinship matrix",
            description="Relatedness matrix, required by the mixed model.",
            allowed_extensions=(".json",),
        ),
        CapabilityInput(
            key="components",
            name="Principal components",
            description="Ancestry axes, used as covariates when supplied.",
            allowed_extensions=(".json",),
        ),
    ),
    # Only the cohort is structurally required; grm and components are optional
    # context whose absence is a *model* error, not a contract error. That
    # distinction is the point: the broker cannot know that mlm needs a GRM, so
    # the runner is the thing that refuses.
    input_variants=(("cohort",),),
    outputs=(Port(name="stats", artifact_type="json"),),
    parameters={
        "model": Parameter(
            type="string",
            description="Association model.",
            enum=("linear", "mlm"),
            default="linear",
        ),
        "covariate_components": Parameter(
            type="integer",
            description="How many ancestry axes to fit as fixed covariates.",
            minimum=0,
            maximum=10,
            default=0,
        ),
    },
    timeout_seconds=60,
    tags=("gwas", "association", "regression", "scan", "mlm"),
)


@registry.capability_runner(ASSOCIATE)
async def associate(ctx: NodeContext) -> NodeResult:
    cohort = read_cohort(ctx.input("cohort").read_text())
    model = ctx.parameters["model"]

    if model == "mlm" and not ctx.has_input("grm"):
        # A genuine, non-retryable failure: re-running changes nothing, the plan
        # has to change. This is what the agent replans against.
        return NodeResult.fail(
            "model='mlm' needs a kinship matrix on the 'grm' input; "
            "run gwas.kinship first and feed its artifact in"
        )

    phenotype = [sample["phenotype"] for sample in cohort["samples"]]
    covariates: list[list[float]] = []

    if model == "mlm":
        grm = json.loads(ctx.input("grm").read_text())
        wanted = max(2, int(ctx.parameters["covariate_components"]) or 2)
        covariates = power_iteration(grm["matrix"], wanted)
        covariates = [[axis[row] for row in range(len(phenotype))] for axis in covariates]
    elif ctx.has_input("components") and int(ctx.parameters["covariate_components"]):
        scores = json.loads(ctx.input("components").read_text())["scores"]
        wanted = int(ctx.parameters["covariate_components"])
        covariates = [[row["pc"][axis] for row in scores] for axis in range(wanted)]

    # Residualise the phenotype on the covariates, then test each marker against
    # what is left. Sequential residualisation is enough for the near-orthogonal
    # axes power iteration produces.
    adjusted = list(phenotype)
    for covariate in covariates:
        beta, _, _ = regress(covariate, adjusted)
        mu_c, mu_y = mean(covariate), mean(adjusted)
        adjusted = [
            adjusted[i] - (mu_y - beta * mu_c + beta * covariate[i])
            for i in range(len(adjusted))
        ]

    results: list[dict[str, Any]] = []
    for index, marker in enumerate(cohort["markers"]):
        column = [float(sample["genotypes"][index]) for sample in cohort["samples"]]
        beta, stderr, p = regress(column, adjusted)
        results.append(
            {
                "marker": marker,
                "beta": round(beta, 5),
                "se": round(stderr, 5) if math.isfinite(stderr) else None,
                "p": p,
            }
        )
        ctx.progress((index + 1) / len(cohort["markers"]), f"tested {marker}")

    inflation = chi2_median_from_p([row["p"] for row in results])
    ctx.log(f"genomic inflation lambda = {inflation:.3f}", "info")

    ctx.emit(
        "stats",
        f"assoc.{model}.json",
        json.dumps(
            {
                "model": model,
                "covariates": len(covariates),
                "lambda_gc": round(inflation, 4),
                "results": results,
            },
            indent=2,
        ),
    )
    return NodeResult.ok(
        model=model,
        markers_tested=len(results),
        covariates=len(covariates),
        lambda_gc=round(inflation, 4),
    )


# ── 5. Multiple-testing correction ──────────────────────────────────────────

CORRECT = Capability(
    id="gwas.correct",
    name="Multiple-testing correction",
    description=(
        "Apply Bonferroni and Benjamini-Hochberg to an association scan and "
        "report which markers survive. Also re-reports the genomic inflation "
        "factor, because a hit list is meaningless without it."
    ),
    runner="gwas.correct",
    inputs=(
        CapabilityInput(
            key="stats",
            name="Association statistics",
            description="Output of gwas.associate.",
            allowed_extensions=(".json",),
        ),
    ),
    outputs=(Port(name="hits", artifact_type="json"),),
    parameters={
        "alpha": Parameter(
            type="number",
            description="Family-wise error rate.",
            minimum=1e-6,
            maximum=0.5,
            default=0.05,
        )
    },
    tags=("gwas", "multiple-testing", "bonferroni", "fdr"),
)


@registry.capability_runner(CORRECT)
async def correct(ctx: NodeContext) -> NodeResult:
    scan = json.loads(ctx.input("stats").read_text())
    alpha = float(ctx.parameters["alpha"])
    results = sorted(scan["results"], key=lambda row: row["p"])
    total = len(results)
    bonferroni = alpha / total if total else alpha

    hits: list[dict[str, Any]] = []
    fdr_threshold = 0.0
    for rank, row in enumerate(results, start=1):
        if row["p"] <= rank / total * alpha:
            fdr_threshold = row["p"]
    for row in results:
        passes_bonferroni = row["p"] <= bonferroni
        passes_fdr = row["p"] <= fdr_threshold
        if passes_bonferroni or passes_fdr:
            hits.append({**row, "bonferroni": passes_bonferroni, "fdr": passes_fdr})

    ctx.emit(
        "hits",
        "hits.json",
        json.dumps(
            {
                "model": scan["model"],
                "lambda_gc": scan["lambda_gc"],
                "markers_tested": total,
                "bonferroni_threshold": bonferroni,
                "fdr_threshold": fdr_threshold,
                "hits": hits,
            },
            indent=2,
        ),
    )
    return NodeResult.ok(
        hits=len(hits),
        bonferroni_hits=sum(1 for row in hits if row["bonferroni"]),
        lambda_gc=scan["lambda_gc"],
    )


# ── 6. Gene annotation (the flaky external service) ─────────────────────────

_ANNOTATION_ATTEMPTS: dict[str, int] = {}

# The three markers `data.py` gives a genuine effect map to salt-tolerance
# genes; everything else in the catalogue is intergenic. A hit that resolves to
# "no annotated gene" is not automatically wrong, but it is the first thing a
# reviewer will ask about, so the annotation says so rather than staying silent.
GENE_TABLE = {
    "rs1385": ("HKT1;5", "root sodium transporter"),
    "rs2309": ("DREB2A", "dehydration-responsive element binding factor"),
    "rs3233": ("NHX1", "vacuolar Na+/H+ antiporter"),
    "rs4157": ("SOS1", "salt-overly-sensitive plasma-membrane antiporter"),
}

ANNOTATE = Capability(
    id="gwas.annotate",
    name="Annotate hits against the gene catalogue",
    description=(
        "Look up the nearest annotated gene for each surviving marker in the "
        "external catalogue service. The service is rate-limited, so this "
        "capability retries with exponential backoff."
    ),
    runner="gwas.annotate",
    inputs=(
        CapabilityInput(
            key="hits",
            name="Surviving markers",
            description="Output of gwas.correct.",
            allowed_extensions=(".json",),
        ),
    ),
    outputs=(Port(name="annotated", artifact_type="json"),),
    # The engine re-runs this up to three times, waiting 0.2s then 0.4s.
    max_attempts=3,
    retry_backoff_seconds=0.2,
    timeout_seconds=15,
    parameters={
        "build": Parameter(
            type="string", description="Genome build to resolve against.", default="v4"
        )
    },
    tags=("gwas", "annotation", "gene", "catalogue", "lookup"),
)


@registry.capability_runner(ANNOTATE)
async def annotate(ctx: NodeContext) -> NodeResult:
    """Simulates a rate-limited catalogue: the first two attempts get a 503."""
    build = ctx.parameters["build"]
    _ANNOTATION_ATTEMPTS[build] = _ANNOTATION_ATTEMPTS.get(build, 0) + 1
    if _ANNOTATION_ATTEMPTS[build] < 3:
        ctx.log(f"gene catalogue rate-limited (attempt {ctx.attempt})", "warn")
        # `retry` marks this as worth another attempt; a plain `fail` would not.
        return NodeResult.retry(f"catalogue returned 503 (attempt {ctx.attempt})")

    payload = json.loads(ctx.input("hits").read_text())
    annotated = []
    for hit in payload["hits"]:
        gene, description = GENE_TABLE.get(hit["marker"], ("—", "no annotated gene within 50kb"))
        annotated.append({**hit, "gene": gene, "gene_description": description})

    ctx.emit(
        "annotated",
        "annotated-hits.json",
        json.dumps(
            {**payload, "build": build, "attempts_used": ctx.attempt, "hits": annotated},
            indent=2,
        ),
    )
    return NodeResult.ok(annotated=len(annotated), attempts_used=ctx.attempt)


# ── 7. The manuscript-grade summary (fan-in) ────────────────────────────────

SUMMARISE = Capability(
    id="gwas.summarise",
    name="Compose the study summary",
    description=(
        "Combine the QC report, the population-structure evidence and the "
        "annotated hit list into a Markdown summary of the study."
    ),
    runner="gwas.summarise",
    inputs=(
        CapabilityInput(key="qc_report", name="QC report", description="Output of gwas.qc.", allowed_extensions=(".json",)),
        CapabilityInput(key="annotated", name="Annotated hits", description="Output of gwas.annotate.", allowed_extensions=(".json",)),
        CapabilityInput(key="components", name="Ancestry axes", description="Output of gwas.pca.", allowed_extensions=(".json",)),
    ),
    # The PCA artifact is optional context; the other two are required.
    input_variants=(("qc_report", "annotated"),),
    outputs=(Port(name="summary", artifact_type="md"),),
    tags=("gwas", "report", "markdown", "summary"),
)


@registry.capability_runner(SUMMARISE)
async def summarise(ctx: NodeContext) -> NodeResult:
    qc_report = json.loads(ctx.input("qc_report").read_text())
    payload = json.loads(ctx.input("annotated").read_text())
    structure = (
        json.loads(ctx.input("components").read_text()) if ctx.has_input("components") else {}
    )

    inflation = payload["lambda_gc"]
    lines = [
        "# Association study summary",
        "",
        "## Cohort",
        "",
        f"- Source: **{qc_report['source']}**",
        f"- Samples: **{qc_report['samples_out']}** of {qc_report['samples_in']} "
        f"({qc_report['dropped_samples']} dropped on call rate)",
        f"- Markers: **{qc_report['markers_out']}** of {qc_report['markers_in']} "
        f"({len(qc_report['dropped_markers'])} dropped on MAF or call rate)",
        "",
        "## Model",
        "",
        f"- Association model: **{payload['model']}**",
        f"- Genomic inflation factor: **λ = {inflation}**",
    ]

    if structure:
        lines.append(
            f"- PC1 separates the ancestry groups by "
            f"**{structure['pc1_ancestry_separation']} SD**"
        )

    lines += ["", "## Verdict", ""]
    if inflation > 1.15:
        lines.append(
            f"⚠ λ = {inflation} is well above 1. The test statistics are inflated "
            "across the whole genome, which is the signature of population "
            "structure rather than of many true associations. **The hit list "
            "below should not be read as discoveries.**"
        )
    else:
        lines.append(
            f"✅ λ = {inflation} is consistent with a well-calibrated null. "
            "Surviving markers can be read as associations."
        )

    lines += [
        "",
        "## Surviving markers",
        "",
        f"Tested {payload['markers_tested']} markers; Bonferroni threshold "
        f"p ≤ {payload['bonferroni_threshold']:.3g}.",
        "",
        "| Marker | β | p | Bonferroni | FDR | Nearest gene |",
        "| --- | ---: | ---: | :---: | :---: | --- |",
    ]
    for hit in payload["hits"]:
        lines.append(
            f"| `{hit['marker']}` | {hit['beta']:+.3f} | {hit['p']:.3g} | "
            f"{'✅' if hit['bonferroni'] else '—'} | {'✅' if hit['fdr'] else '—'} | "
            f"{hit['gene']} — {hit['gene_description']} |"
        )
    if not payload["hits"]:
        lines.append("| — | — | — | — | — | nothing survived correction |")

    ctx.emit("summary", "study-summary.md", "\n".join(lines) + "\n")
    return NodeResult.ok(hit_count=len(payload["hits"]), lambda_gc=inflation)


# ── 8. Registering a finding (human approval) ───────────────────────────────

REGISTER = Capability(
    id="gwas.register_finding",
    name="Register the locus as a finding",
    description=(
        "Submit the surviving locus to the shared discovery register, where it "
        "becomes a claim other groups will build on. Outward-facing and hard to "
        "retract, so it requires human approval first."
    ),
    runner="gwas.register_finding",
    inputs=(
        CapabilityInput(
            key="summary",
            name="Study summary",
            description="The Markdown summary backing the claim.",
            allowed_extensions=(".md",),
        ),
    ),
    outputs=(Port(name="receipt", artifact_type="json"),),
    requires_approval=True,
    tags=("gwas", "register", "publish", "discovery"),
)


@registry.capability_runner(REGISTER)
async def register_finding(ctx: NodeContext) -> NodeResult:
    # Because the capability declares `requires_approval=True`, the engine parks
    # this node *before* calling us. Reaching this line means a person already
    # said yes — `ctx.config["approved"]` is True — so there is no guard to
    # write here. The gate is in front of the side effect, not around it.
    ctx.emit(
        "receipt",
        "register-receipt.json",
        json.dumps({"registered": True, "register": "shared-discovery-v4"}, indent=2),
    )
    return NodeResult.ok()


# ── A registered workflow (a fixed, pre-approved SOP) ───────────────────────

STRUCTURED_SOP = Workflow(
    id="gwas.structured_scan",
    name="Structure-aware association scan",
    description=(
        "The lab's standard scan: QC, then ancestry and relatedness computed in "
        "parallel, then a kinship-corrected association and correction. Offered "
        "as one unit for studies that want the accepted protocol with no "
        "deviation."
    ),
    inputs=(
        CapabilityInput(
            key="vcf",
            name="VCF",
            description="The cohort to scan.",
            allowed_extensions=(".vcf",),
        ),
    ),
    nodes=(
        WorkflowNode(
            id="qc",
            name="Quality control",
            runner="gwas.qc",
            inputs=("vcf",),
            outputs=(
                Port(name="cohort", artifact_type="tsv"),
                Port(name="qc_report", artifact_type="json"),
            ),
        ),
        # These two declare the same single dependency, so the engine runs them
        # concurrently. The SOP author never opts in to parallelism — the shape
        # of the graph is the opt-in.
        WorkflowNode(
            id="pca",
            name="Ancestry axes",
            runner="gwas.pca",
            depends_on=("qc",),
            outputs=(Port(name="components", artifact_type="json"),),
        ),
        WorkflowNode(
            id="kinship",
            name="Relatedness",
            runner="gwas.kinship",
            depends_on=("qc",),
            outputs=(Port(name="grm", artifact_type="json"),),
        ),
        WorkflowNode(
            id="assoc",
            name="Association scan",
            runner="gwas.associate",
            depends_on=("qc", "pca", "kinship"),
            outputs=(Port(name="stats", artifact_type="json"),),
        ),
        WorkflowNode(
            id="correct",
            name="Correction",
            runner="gwas.correct",
            depends_on=("assoc",),
            outputs=(Port(name="hits", artifact_type="json"),),
        ),
    ),
    parameters={
        "min_maf": Parameter(
            type="number", description="Minor-allele frequency floor.", minimum=0, maximum=0.5, default=0.05
        ),
        "min_call_rate": Parameter(
            type="number", description="Call-rate floor.", minimum=0, maximum=1, default=0.9
        ),
        "components": Parameter(
            type="integer", description="Ancestry axes to retain.", minimum=1, maximum=10, default=2
        ),
        "model": Parameter(
            type="string", description="Association model.", enum=("linear", "mlm"), default="mlm"
        ),
        "covariate_components": Parameter(
            type="integer", description="Axes fitted as covariates.", minimum=0, maximum=10, default=2
        ),
        "alpha": Parameter(
            type="number", description="Family-wise error rate.", minimum=1e-6, maximum=0.5, default=0.05
        ),
    },
    tags=("gwas", "sop", "structure-aware"),
)

registry.register_workflow(STRUCTURED_SOP)


def reset_annotation_attempts() -> None:
    """Test helper: make the rate-limited catalogue start failing again."""
    _ANNOTATION_ATTEMPTS.clear()


assert not registry.validate(), registry.validate()
