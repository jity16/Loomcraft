"""Example 2 — a document research assistant.

Where example 1 is a fixed analytical pipeline, this one is about the messier
half of agent work: the agent does not know up front what it has, has to *ask*
for what is missing, does some work itself in its own sandbox, and has to change
plan when a step fails.

Capabilities here deliberately include one that **fails on a specific input**, so
the replan path is a real path and not a narrated one.
"""

from __future__ import annotations

import json
import re
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
)

registry = Registry()

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "from", "has",
    "have", "in", "is", "it", "its", "of", "on", "or", "that", "the", "this",
    "to", "was", "were", "which", "will", "with", "we", "our", "their", "they",
}

WORD = re.compile(r"[A-Za-z][A-Za-z'-]{2,}")
SENTENCE = re.compile(r"(?<=[.!?])\s+")


def sentences(text: str) -> list[str]:
    return [item.strip() for item in SENTENCE.split(text) if item.strip()]


# ── 1. Extract ──────────────────────────────────────────────────────────────

EXTRACT = Capability(
    id="docs.extract",
    name="Extract document text",
    description=(
        "Normalise one or more source documents into plain text with per-document "
        "metadata. Accepts .txt and .md."
    ),
    runner="docs.extract",
    inputs=(
        CapabilityInput(
            key="documents",
            name="Documents",
            description="Between one and six source documents.",
            allowed_extensions=(".txt", ".md"),
            max_files=6,
        ),
    ),
    outputs=(Port(name="corpus", artifact_type="json"),),
    tags=("documents", "extract", "text", "parse"),
)


@registry.capability_runner(EXTRACT)
async def extract(ctx: NodeContext) -> NodeResult:
    documents = []
    files = ctx.input_list("documents")
    for index, item in enumerate(files):
        text = item.read_text()
        documents.append(
            {
                "filename": item.filename,
                "characters": len(text),
                "words": len(WORD.findall(text)),
                "sentences": len(sentences(text)),
                "text": text,
            }
        )
        ctx.progress((index + 1) / len(files), f"extracted {item.filename}")

    ctx.emit("corpus", "corpus.json", json.dumps({"documents": documents}, indent=2))
    return NodeResult.ok(
        document_count=len(documents),
        total_words=sum(item["words"] for item in documents),
    )


# ── 2. Summarise ────────────────────────────────────────────────────────────

SUMMARISE = Capability(
    id="docs.summarise",
    name="Summarise the corpus",
    description=(
        "Produce an extractive summary by scoring sentences on distinctive term "
        "frequency. Deterministic, so it is auditable."
    ),
    runner="docs.summarise",
    inputs=(
        CapabilityInput(
            key="corpus",
            name="Corpus",
            description="Extracted corpus JSON.",
            allowed_extensions=(".json",),
        ),
    ),
    outputs=(Port(name="summary", artifact_type="md"),),
    parameters={
        "sentences_per_document": Parameter(
            type="integer",
            description="How many sentences to keep per document.",
            minimum=1,
            maximum=10,
            default=3,
        )
    },
    tags=("summary", "documents", "extractive"),
)


@registry.capability_runner(SUMMARISE)
async def summarise(ctx: NodeContext) -> NodeResult:
    corpus = json.loads(ctx.input("corpus").read_text())
    keep = int(ctx.parameters["sentences_per_document"])

    frequencies = Counter(
        word.lower()
        for document in corpus["documents"]
        for word in WORD.findall(document["text"])
        if word.lower() not in STOPWORDS
    )

    lines = ["# Summary", ""]
    for document in corpus["documents"]:
        lines.append(f"## {document['filename']}")
        lines.append("")
        scored = [
            (
                sum(frequencies[word.lower()] for word in WORD.findall(sentence))
                / max(1, len(WORD.findall(sentence))),
                position,
                sentence,
            )
            for position, sentence in enumerate(sentences(document["text"]))
        ]
        # Rank by score, then restore reading order so the summary flows.
        best = sorted(sorted(scored, reverse=True)[:keep], key=lambda row: row[1])
        lines.extend(f"- {sentence}" for _, _, sentence in best)
        lines.append("")

    ctx.emit("summary", "summary.md", "\n".join(lines))
    return NodeResult.ok(document_count=len(corpus["documents"]))


# ── 3. Themes ───────────────────────────────────────────────────────────────

THEMES = Capability(
    id="docs.themes",
    name="Identify shared themes",
    description="Rank terms appearing across multiple documents to surface common themes.",
    runner="docs.themes",
    inputs=(
        CapabilityInput(
            key="corpus",
            name="Corpus",
            description="Extracted corpus JSON.",
            allowed_extensions=(".json",),
        ),
    ),
    outputs=(Port(name="themes", artifact_type="json"),),
    parameters={
        "top_n": Parameter(
            type="integer", description="How many themes to return.", minimum=1, maximum=30, default=8
        )
    },
    tags=("themes", "topics", "keywords", "documents"),
)


@registry.capability_runner(THEMES)
async def themes(ctx: NodeContext) -> NodeResult:
    corpus = json.loads(ctx.input("corpus").read_text())
    documents = corpus["documents"]

    per_document: list[Counter[str]] = [
        Counter(
            word.lower()
            for word in WORD.findall(document["text"])
            if word.lower() not in STOPWORDS
        )
        for document in documents
    ]

    scored: list[dict[str, Any]] = []
    for term in {word for counter in per_document for word in counter}:
        appearances = sum(1 for counter in per_document if counter[term])
        if appearances < 2 and len(documents) > 1:
            continue  # a term in one document is not a shared theme
        scored.append(
            {
                "term": term,
                "documents": appearances,
                "occurrences": sum(counter[term] for counter in per_document),
            }
        )

    scored.sort(key=lambda row: (-row["documents"], -row["occurrences"], row["term"]))
    top = scored[: int(ctx.parameters["top_n"])]
    ctx.emit("themes", "themes.json", json.dumps({"themes": top}, indent=2))
    return NodeResult.ok(theme_count=len(top))


# ── 4. Contradictions (the capability that fails) ───────────────────────────

CONTRADICTIONS = Capability(
    id="docs.contradictions",
    name="Find contradictions",
    description=(
        "Detect statements that contradict each other across documents. Requires "
        "at least two documents — with one there is nothing to compare against."
    ),
    runner="docs.contradictions",
    inputs=(
        CapabilityInput(
            key="corpus",
            name="Corpus",
            description="Extracted corpus JSON.",
            allowed_extensions=(".json",),
        ),
    ),
    outputs=(Port(name="contradictions", artifact_type="json"),),
    tags=("contradictions", "conflict", "compare", "verify"),
)


@registry.capability_runner(CONTRADICTIONS)
async def contradictions(ctx: NodeContext) -> NodeResult:
    corpus = json.loads(ctx.input("corpus").read_text())
    documents = corpus["documents"]

    if len(documents) < 2:
        # A precondition failure, not a transient one — retrying is pointless,
        # so this is `fail`, not `retry`. The agent's move is to replan.
        return NodeResult.fail(
            "contradiction analysis needs at least two documents; "
            f"the corpus has {len(documents)}"
        )

    negation = re.compile(r"\b(not|never|no longer|cannot|failed to|declined)\b", re.I)
    claims: list[tuple[str, str]] = [
        (document["filename"], sentence)
        for document in documents
        for sentence in sentences(document["text"])
    ]

    findings = []
    for index, (source_a, sentence_a) in enumerate(claims):
        terms_a = {word.lower() for word in WORD.findall(sentence_a)} - STOPWORDS
        for source_b, sentence_b in claims[index + 1 :]:
            if source_a == source_b:
                continue
            terms_b = {word.lower() for word in WORD.findall(sentence_b)} - STOPWORDS
            overlap = terms_a & terms_b
            if len(overlap) < 3:
                continue
            if bool(negation.search(sentence_a)) != bool(negation.search(sentence_b)):
                findings.append(
                    {
                        "shared_terms": sorted(overlap)[:6],
                        "a": {"document": source_a, "claim": sentence_a},
                        "b": {"document": source_b, "claim": sentence_b},
                    }
                )

    ctx.emit(
        "contradictions",
        "contradictions.json",
        json.dumps({"findings": findings[:20]}, indent=2),
    )
    return NodeResult.ok(finding_count=len(findings))


# ── 5. Brief ────────────────────────────────────────────────────────────────

BRIEF = Capability(
    id="docs.brief",
    name="Compose a research brief",
    description="Combine the summary and themes (and contradictions, if available) into a brief.",
    runner="docs.brief",
    inputs=(
        CapabilityInput(key="summary", name="Summary", description="Summary Markdown.", allowed_extensions=(".md",)),
        CapabilityInput(key="themes", name="Themes", description="Themes JSON.", allowed_extensions=(".json",)),
        CapabilityInput(
            key="contradictions",
            name="Contradictions",
            description="Optional contradictions JSON.",
            allowed_extensions=(".json",),
        ),
    ),
    input_variants=(("summary", "themes"),),
    outputs=(Port(name="brief", artifact_type="md"),),
    parameters={
        "title": Parameter(type="string", description="Brief title.", default="Research brief")
    },
    tags=("brief", "report", "synthesis"),
)


@registry.capability_runner(BRIEF)
async def brief(ctx: NodeContext) -> NodeResult:
    summary_text = ctx.input("summary").read_text()
    theme_data = json.loads(ctx.input("themes").read_text())
    conflict_data = (
        json.loads(ctx.input("contradictions").read_text())
        if ctx.has_input("contradictions")
        else None
    )

    lines = [f"# {ctx.parameters['title']}", ""]

    lines += ["## Shared themes", ""]
    if theme_data["themes"]:
        for theme in theme_data["themes"]:
            lines.append(
                f"- **{theme['term']}** — in {theme['documents']} document(s), "
                f"{theme['occurrences']} mention(s)"
            )
    else:
        lines.append("_No term appeared across multiple documents._")

    lines += ["", "## Contradictions", ""]
    if conflict_data is None:
        lines.append(
            "_Not analysed — contradiction detection needs at least two documents._"
        )
    elif conflict_data["findings"]:
        for finding in conflict_data["findings"]:
            lines.append(f"- `{finding['a']['document']}`: {finding['a']['claim']}")
            lines.append(f"  vs `{finding['b']['document']}`: {finding['b']['claim']}")
    else:
        lines.append("_No contradictions detected._")

    lines += ["", summary_text]
    ctx.emit("brief", "research-brief.md", "\n".join(lines) + "\n")
    return NodeResult.ok(
        has_contradiction_section=conflict_data is not None,
        theme_count=len(theme_data["themes"]),
    )


assert not registry.validate(), registry.validate()
