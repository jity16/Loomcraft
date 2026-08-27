# Examples

Two runnable scenarios. Both work with **no API key** — a scripted agent replays
the exact tool calls a model would make, and every one still goes through the
real broker, engine, and event log.

Both are scientific, and in both the interesting moment is the same: **the agent
gets a defensible-looking answer, checks it, and finds it does not hold.** That
is what an agent-authored plan is for. A fixed pipeline cannot notice that its
own model was misspecified, because it has no way to change what it does next.

```bash
pip install -e packages/core          # or: pip install loomcraft

python examples/01-gwas-discovery/run_scripted.py
python examples/02-literature-meta/run_scripted.py
```

## What each one covers

| Capability | Example 1 | Example 2 |
| --- | :---: | :---: |
| DAG validation (cycles, unknown deps, duplicate ids) | ✅ | |
| Dependency layering → parallel scheduling | ✅ | ✅ |
| Real concurrent execution inside one graph | ✅ | |
| Dependency gating (no jumping ahead) | ✅ | ✅ |
| Terminal-state protection (`succeeded` cannot restart) | ✅ | |
| Typed input contracts and input variants | ✅ | ✅ |
| Optional inputs (one capability, two shapes of output) | ✅ | ✅ |
| Parameter validation (types, ranges, enums) | ✅ | |
| Port-addressed artifacts (two outputs, no positional guessing) | ✅ | |
| Retry with exponential backoff | ✅ | |
| Timeouts | ✅ | |
| Human approval before a hard-to-reverse step | ✅ | |
| Genuine step failure, for a domain reason | ✅ | ✅ |
| Skip propagation to the downstream subtree | ✅ | ✅ |
| Replan discipline (increasing revision + reason) | ✅ | ✅ |
| Artifact survival across a replan | ✅ | ✅ |
| Structured file requests + execution gating | | ✅ |
| Upload allocation across typed slots | | ✅ |
| Agent-reported `review` / `answer` steps | ✅ | ✅ |
| Hash-chained audit log | ✅ | ✅ |
| HTTP + SSE server | ✅ | |
| Browser UI | ✅ | |
| Live Claude agent | | ✅ |

## [01 · Association study](01-gwas-discovery/)

A genome-wide association scan that **discovers its own first answer was wrong.**

The naive per-marker scan runs fine and returns a hit list. It is also
meaningless: the genomic inflation factor comes back at λ = 2.80, which says the
whole test-statistic distribution is shifted rather than a few loci standing out.
Five of the eight surviving markers are ancestry in disguise. A `review` step
reads λ off the artifact, and the agent publishes revision 2 with a kinship-
corrected mixed model. λ falls to 0.95 and exactly the three markers that carry a
real effect survive.

None of that is scripted. `data.py` builds a cohort in which ancestry moves both
the phenotype and most allele frequencies, so the confounding — and its cure —
are arithmetic, not narration.

```bash
python examples/01-gwas-discovery/run_scripted.py      # 16 annotated sections

pip install 'loomcraft[server]'
python examples/01-gwas-discovery/serve.py --write-cohort   # sample data
python examples/01-gwas-discovery/serve.py --scripted       # http://127.0.0.1:8000
```

The browser UI in `web/index.html` is a single dependency-free file that ports
the reducer, the layout, and the design tokens from `@loomcraft/renderer`. Diff
it against `packages/renderer/src/state.ts` to see how small the front-end
contract really is.

## [02 · Literature meta-analysis](02-literature-meta/)

An evidence review that has to **ask for what it is missing**, and that finds the
headline number is one study wearing a trenchcoat.

The user uploads two trial reports. Two is not enough to pool — between-study
variance estimated from one degree of freedom is not an estimate — so the agent
calls `request_inputs` and stops; everything it tries afterwards is refused until
the request is answered.

Given all five trials, pooling, leave-one-out influence and Egger's regression
fan out from one parent and back in. The pooled effect is +8.5% and looks solid.
The influence analysis shows that removing the smallest study drops it to +5.4%
**and takes the heterogeneity from I² = 74% to exactly zero.** The funnel is
asymmetric in precisely the direction that predicts.

Decline the request instead and you get the other branch: `lit.meta` fails for a
real statistical reason, its dependents are skipped, and the agent replans to a
narrative synthesis that says plainly it is not a meta-analysis.

```bash
python examples/02-literature-meta/run_scripted.py   # both branches

pip install 'loomcraft[anthropic]'
export ANTHROPIC_API_KEY=...                         # or: ant auth login
python examples/02-literature-meta/run_live.py
python examples/02-literature-meta/run_live.py --partial --decline
```

`run_live.py` and `run_scripted.py` share the same capabilities, broker, and
session. The only difference is who chooses the next tool call — which is the
argument for developing against the scripted agent and switching one line for
production.
