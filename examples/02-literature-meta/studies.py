"""Five study reports, in the shape a screening tool would hand you.

The numbers are constructed, but they are constructed to be *coherent*: standard
errors scale roughly with 1/sqrt(n), and the arithmetic in ``capabilities.py`` is
the real DerSimonian-Laird estimator, so the finding the example arrives at is
computed rather than asserted.

What that finding is: pooled across all five, salt-tolerance treatment looks
worth +8.5% yield with a confident interval that excludes zero. It is not.
Silva 2023 — the smallest study, with by far the largest effect — accounts for
the entire excess. Drop it and the pooled effect falls to +5.4% and the
between-study heterogeneity goes from I-squared = 74% to exactly zero. Egger's
regression points the same way from a different angle.

Two of these reports are the ones the user starts with. The other three are what
the agent has to *ask* for, which is what makes ``request_inputs`` a real branch
rather than a decoration.
"""

from __future__ import annotations

REPORTS: dict[str, str] = {
    "nakamura-2021.txt": """\
Study: Nakamura 2021
Design: randomised block, two sites, single season
Samples: 148
Outcome: grain yield under 100 mM NaCl
Effect: +7.2% (95% CI 0.9 to 13.5)
Notes: Both sites reported separately in the appendix; effect is the pooled
site estimate. Pre-registered. No funding from seed suppliers declared.
""",
    "petrov-2020.txt": """\
Study: Petrov 2020
Design: multi-site randomised, four sites, two seasons
Samples: 412
Outcome: grain yield under 100 mM NaCl
Effect: +4.6% (95% CI 0.9 to 8.3)
Notes: The largest trial in the set and the only one with a second season.
Reports a smaller effect in season two than season one.
""",
    "oduya-2022.txt": """\
Study: Oduya 2022
Design: randomised, single site
Samples: 96
Outcome: grain yield under 100 mM NaCl
Effect: +5.9% (95% CI -1.9 to 13.7)
Notes: Interval crosses zero. Authors describe the result as suggestive and
call for replication at scale.
""",
    "silva-2023.txt": """\
Study: Silva 2023
Design: randomised, single site, greenhouse
Samples: 63
Outcome: grain yield under 100 mM NaCl
Effect: +24.8% (95% CI 15.2 to 34.4)
Notes: Greenhouse rather than field. Smallest sample in the set and by some
margin the largest reported effect.
""",
    "whitfield-2019.txt": """\
Study: Whitfield 2019
Design: randomised, two sites
Samples: 210
Outcome: grain yield under 100 mM NaCl
Effect: +5.4% (95% CI 0.1 to 10.7)
Notes: Interval only just excludes zero. Consistent across both sites.
""",
}

# What the user uploads without being asked.
STARTING = ("nakamura-2021.txt", "petrov-2020.txt")

# What the agent has to request, because the pooled analysis is not estimable
# from two studies.
REQUESTED = ("oduya-2022.txt", "silva-2023.txt", "whitfield-2019.txt")

# The study the influence analysis should single out. Named here so the example
# can check that it actually did, rather than printing whatever it found.
EXPECTED_OUTLIER = "Silva 2023"
