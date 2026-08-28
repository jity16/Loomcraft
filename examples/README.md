# Examples

Four runnable scenarios. All work with **no API key** — a scripted agent
replays the exact tool calls a model would make, and every one still goes
through the real broker, engine, and event log.

The first is a compact workbench tour. The next two are scientific, and share
the same interesting moment: **the agent gets a defensible-looking answer,
checks it, and finds it does not hold.** That is what an agent-authored plan is
for. A fixed pipeline cannot
notice that its own model was misspecified, because it has no way to change what
it does next.

The fourth is about the other half of honest investigation: what happens to a
question the data simply cannot answer.

```bash
git clone https://github.com/jity16/Loomcraft.git && cd Loomcraft
pip install -e packages/core

python examples/00-workbench-tour/run.py
python examples/01-gwas-discovery/run_scripted.py
python examples/02-literature-meta/run_scripted.py
python examples/03-objectives-and-scheduling/run.py
```

## What each one covers

| Capability | Tour | Example 1 | Example 2 | Example 3 |
| --- | :---: | :---: | :---: | :---: |
| DAG validation (cycles, unknown deps, duplicate ids) | ✅ | ✅ | | |
| Dependency layering → parallel scheduling | ✅ | ✅ | ✅ | ✅ |
| Real concurrent execution inside one graph | ✅ | ✅ | | ✅ |
| Dependency gating (no jumping ahead) | | ✅ | ✅ | ✅ |
| Terminal-state protection (`succeeded` cannot restart) | | ✅ | | ✅ |
| Typed input contracts and input variants | ✅ | ✅ | ✅ | |
| Optional inputs (one capability, two shapes of output) | | ✅ | ✅ | |
| Parameter validation (types, ranges, enums) | | ✅ | | ✅ |
| Port-addressed artifacts (two outputs, no positional guessing) | ✅ | ✅ | | ✅ |
| Retry with exponential backoff | ✅ | ✅ | | ✅ |
| Timeouts | | ✅ | | ✅ |
| Human approval before a hard-to-reverse step | ✅ | ✅ | | |
| Genuine step failure, for a domain reason | | ✅ | ✅ | ✅ |
| Skip propagation to the downstream subtree | | ✅ | ✅ | |
| Replan discipline (increasing revision + reason) | | ✅ | ✅ | ✅ |
| Artifact survival across a replan | | ✅ | ✅ | |
| Structured file requests + execution gating | | | ✅ | |
| Upload allocation across typed slots | | | ✅ | |
| Agent-reported `review` / `answer` steps | | ✅ | ✅ | |
| Hash-chained audit log | ✅ | ✅ | ✅ | ✅ |
| HTTP + SSE server | | ✅ | | |
| Browser UI | | ✅ | | |
| Live Claude agent | | | ✅ | |
| Declared objectives + evidence ledger | | | | ✅ |
| Whole-plan execution (`execute_plan`) | ✅ | | | ✅ |
| `on_failure: continue` — a tolerated failure | | | | ✅ |
| Server-owned `review` bound to a capability | | | | ✅ |
| Codex / app-server JSON-RPC bridge | | | | ✅ |

## [00 · Workbench tour](00-workbench-tour/)

The shortest path to the distinctive bit: one upload is normalised once, then
three independent branches run concurrently, each gets a quality check, and a
single report waits behind an approval gate. The script prints the measured
overlap, retry count, final statuses, and hash-chain result.

```bash
python examples/00-workbench-tour/run.py
```

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

pip install -e "packages/core[server]"
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

pip install -e "packages/core[anthropic]"
export ANTHROPIC_API_KEY=...                         # or: ant auth login
python examples/02-literature-meta/run_live.py
python examples/02-literature-meta/run_live.py --partial --decline
```

`run_live.py` and `run_scripted.py` share the same capabilities, broker, and
session. The only difference is who chooses the next tool call — which is the
argument for developing against the scripted agent and switching one line for
production.

## [03 · Objectives and scheduling](03-objectives-and-scheduling/)

A field trial where **one of the two questions asked cannot be answered**, and
the record has to say so.

The breeder asks which genotypes yield best, and whether there is a maternal
effect. The first is answerable. The second is not: the trial table has no dam
column, so the maternal variance is not identifiable however the analysis is
arranged. That branch is marked `on_failure: "continue"`, so when it fails the
annotation and review steps hanging off a *different* branch still run — the run
finishes `succeeded` with the failure recorded rather than cascading.

Then the agent tries to tidy up by publishing a revision that quietly drops the
maternal question, and the server refuses. The revision it eventually publishes
marks that objective `not_estimable`, states why, and names what would change
it: a pedigree export with dam ids.

The whole plan runs in a single `execute_plan` call — six nodes, three of them
in one concurrent layer, with retry driven from the plan and a `review` step
bound to a review-scoped capability so the verdict is the server's rather than
the agent's. The last section drives the same broker over JSON-RPC, the way a
Codex app-server would.

```bash
python examples/03-objectives-and-scheduling/run.py   # 8 annotated sections
```
