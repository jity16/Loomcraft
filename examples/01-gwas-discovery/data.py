"""A synthetic cohort with a real confounder built into it.

Nothing in this file is faked at the level that matters: the inflation the
example discovers is a genuine statistical consequence of how the cohort is
generated, not a number written into a report.

The generative model is the classic one for population stratification:

    ancestry  ──────────────► phenotype        (a large group mean difference)
        │
        └─────────────────► allele frequency   (drift, at most markers)

Because ancestry moves *both*, every drifted marker is correlated with the
phenotype whether or not it does anything. A naive per-marker scan therefore
reports genome-wide inflation (lambda well above 1) and a long list of
"associations" that are ancestry in disguise. Conditioning on the ancestry axes
removes the shared cause and leaves only the three markers that were given a
real effect.

Everything is seeded, so two runs of the example produce identical numbers and
the documented lambda values stay true.
"""

from __future__ import annotations

import math
import random

MARKER_COUNT = 150
GROUP_SIZE = 60


def marker_name(index: int) -> str:
    return f"rs{1000 + index * 77}"


# Markers given a genuine effect on the phenotype, keyed by position so the name
# scheme and the effects cannot drift apart. These are the ones that should
# survive a correctly specified model — and, in the annotation catalogue in
# capabilities.py, the ones that map to salt-tolerance genes.
CAUSAL_INDICES: dict[int, float] = {5: +2.10, 17: -1.90, 29: +1.70}
CAUSAL: dict[str, float] = {
    marker_name(index): effect for index, effect in CAUSAL_INDICES.items()
}

# These constants are tuned, not arbitrary. They put the naive scan at
# lambda ~= 2.8 with an FDR list that is more than half false positives, and the
# structure-aware scan at lambda ~= 0.97 recovering exactly the three causal
# markers. Moving them moves the numbers quoted in run_scripted.py and the docs.
ANCESTRY_EFFECT = 1.8  # phenotype units between the two groups
NOISE_SD = 2.6


class Draw:
    """A small seeded generator, so the example's numbers never move."""

    def __init__(self, seed: int) -> None:
        self._random = random.Random(seed)

    def uniform(self, low: float, high: float) -> float:
        return low + (high - low) * self._random.random()

    def normal(self, mu: float, sigma: float) -> float:
        # Box-Muller, so this depends only on random(), which is stable.
        u1 = max(self._random.random(), 1e-12)
        u2 = self._random.random()
        return mu + sigma * math.sqrt(-2 * math.log(u1)) * math.cos(2 * math.pi * u2)

    def binomial2(self, p: float) -> int:
        return sum(1 for _ in range(2) if self._random.random() < p)

    def chance(self, p: float) -> bool:
        return self._random.random() < p


def build_cohort(seed: int = 20260827) -> dict[str, object]:
    draw = Draw(seed)

    markers: list[str] = []
    frequencies: list[tuple[float, float]] = []  # (north, south)

    for index in range(MARKER_COUNT):
        markers.append(marker_name(index))
        base = draw.uniform(0.18, 0.5)
        # Most markers drift with ancestry; a minority do not, which is what
        # makes the confounding statistical rather than uniform. The causal
        # markers are held at equal frequency in both groups on purpose: the
        # biology they encode is shared, so conditioning on ancestry must not
        # be able to explain their signal away.
        drift = (
            0.0
            if index in CAUSAL_INDICES
            else (draw.normal(0.0, 0.16) if draw.chance(0.8) else 0.0)
        )
        north = min(max(base + drift, 0.02), 0.98)
        south = min(max(base - drift, 0.02), 0.98)
        frequencies.append((north, south))

    # Two markers are made deliberately unusable, so the QC step has real work:
    # one is nearly monomorphic and one is mostly uncalled.
    monomorphic = markers[7]
    uncalled = markers[19]
    frequencies[7] = (0.012, 0.012)

    samples: list[dict[str, object]] = []
    for group_index, group in enumerate(("north", "south")):
        for member in range(GROUP_SIZE):
            genotypes: list[str] = []
            dosage_by_marker: dict[str, int] = {}
            for index, name in enumerate(markers):
                p = frequencies[index][group_index]
                dosage = draw.binomial2(p)
                dosage_by_marker[name] = dosage
                if name == uncalled and draw.chance(0.75):
                    genotypes.append(".")
                elif draw.chance(0.02):  # ordinary sporadic missingness
                    genotypes.append(".")
                else:
                    genotypes.append(str(dosage))

            phenotype = (
                40.0
                + (ANCESTRY_EFFECT if group == "north" else 0.0)
                + sum(
                    effect * dosage_by_marker[marker_name(index)]
                    for index, effect in CAUSAL_INDICES.items()
                )
                + draw.normal(0.0, NOISE_SD)
            )
            samples.append(
                {
                    "sample": f"S{group_index * GROUP_SIZE + member + 1:03d}",
                    "ancestry": group,
                    "phenotype": phenotype,
                    "genotypes": genotypes,
                }
            )

    return {
        "markers": markers,
        "samples": samples,
        "monomorphic_marker": monomorphic,
        "uncalled_marker": uncalled,
    }


def as_matrix(cohort: dict[str, object]) -> str:
    """The tab-separated matrix both the .bed and the .vcf carry in this example."""
    markers = cohort["markers"]
    lines = ["\t".join(["sample", "ancestry", "phenotype", *markers])]  # type: ignore[misc]
    for sample in cohort["samples"]:  # type: ignore[union-attr]
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


def as_bim(cohort: dict[str, object]) -> str:
    """One line per marker: chromosome, id, position, alleles."""
    lines = []
    for index, marker in enumerate(cohort["markers"]):  # type: ignore[arg-type]
        chromosome = 1 + index % 5
        lines.append(f"{chromosome}\t{marker}\t0\t{(index + 1) * 15000}\tA\tG")
    return "\n".join(lines) + "\n"


def as_fam(cohort: dict[str, object]) -> str:
    """One line per sample: family, individual, parents, sex, phenotype."""
    lines = []
    for sample in cohort["samples"]:  # type: ignore[union-attr]
        lines.append(
            f"FAM1\t{sample['sample']}\t0\t0\t0\t{sample['phenotype']:.4f}"
        )
    return "\n".join(lines) + "\n"
