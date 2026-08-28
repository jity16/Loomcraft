"""Pure DAG algorithms.

This module knows nothing about plans, agents, or execution — it operates on
``{node_id: [dependency_id, ...]}`` adjacency maps.  Keeping it standalone means
the same validation used for agent-authored plans is available to anyone
building a different node model on top of LoomCraft.
"""

from __future__ import annotations

import heapq
from collections import deque
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

Adjacency = Mapping[str, Sequence[str]]


@dataclass(frozen=True, slots=True)
class GraphIssue:
    """One structural problem found in a candidate DAG."""

    kind: str
    node: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.node}: {self.detail}"


def validate(depends_on: Adjacency) -> list[GraphIssue]:
    """Return every structural problem in ``depends_on``.

    Checks, in order: duplicate dependencies, self-dependency, unknown
    dependency targets, and cycles.  An empty list means the graph is a
    well-formed DAG.  Callers that only need a boolean can use :func:`is_dag`.
    """
    issues: list[GraphIssue] = []
    known = set(depends_on)

    for node, dependencies in depends_on.items():
        seen: set[str] = set()
        for dependency in dependencies:
            if dependency == node:
                issues.append(
                    GraphIssue("self_dependency", node, "cannot depend on itself")
                )
                continue
            if dependency in seen:
                issues.append(
                    GraphIssue(
                        "duplicate_dependency",
                        node,
                        f"duplicate dependency {dependency!r}",
                    )
                )
                continue
            seen.add(dependency)
            if dependency not in known:
                issues.append(
                    GraphIssue(
                        "unknown_dependency",
                        node,
                        f"depends on unknown node {dependency!r}",
                    )
                )

    cycle = find_cycle(depends_on)
    if cycle:
        rendered = " -> ".join([*cycle, cycle[0]])
        issues.append(GraphIssue("cycle", cycle[0], f"cycle detected: {rendered}"))
    return issues


def is_dag(depends_on: Adjacency) -> bool:
    """Return whether ``depends_on`` is acyclic and internally consistent."""
    return not validate(depends_on)


def find_cycle(depends_on: Adjacency) -> list[str]:
    """Return one cycle as an ordered node list, or ``[]`` when acyclic.

    Uses an iterative colouring DFS so deep graphs cannot exhaust the Python
    recursion limit — agent-authored plans are bounded, but LoomCraft's graph
    primitives are also used for machine-generated graphs.
    """
    WHITE, GREY, BLACK = 0, 1, 2
    colour = {node: WHITE for node in depends_on}
    parent: dict[str, str | None] = {}

    for root in depends_on:
        if colour[root] != WHITE:
            continue
        parent[root] = None
        stack: list[tuple[str, int]] = [(root, 0)]
        colour[root] = GREY
        while stack:
            node, index = stack.pop()
            dependencies = [
                dependency
                for dependency in depends_on.get(node, ())
                if dependency in colour
            ]
            if index < len(dependencies):
                stack.append((node, index + 1))
                nxt = dependencies[index]
                if colour[nxt] == GREY:
                    cycle = [nxt]
                    walker: str | None = node
                    while walker is not None and walker != nxt:
                        cycle.append(walker)
                        walker = parent.get(walker)
                    cycle.reverse()
                    return cycle
                if colour[nxt] == WHITE:
                    colour[nxt] = GREY
                    parent[nxt] = node
                    stack.append((nxt, 0))
            else:
                colour[node] = BLACK
    return []


def topological_order(depends_on: Adjacency) -> list[str]:
    """Return a stable topological order (Kahn's algorithm, ties broken by id).

    Raises :class:`ValueError` when the graph contains a cycle.
    """
    indegree = {node: 0 for node in depends_on}
    downstream: dict[str, list[str]] = {node: [] for node in depends_on}
    for node, dependencies in depends_on.items():
        unique = [d for d in dict.fromkeys(dependencies) if d in indegree]
        indegree[node] = len(unique)
        for dependency in unique:
            downstream[dependency].append(node)

    # A heap rather than a queue: this yields the lexicographically smallest
    # valid order, so the same DAG always prints the same sequence regardless of
    # the order its nodes were declared in.
    ready = [node for node, degree in indegree.items() if degree == 0]
    heapq.heapify(ready)
    order: list[str] = []
    while ready:
        node = heapq.heappop(ready)
        order.append(node)
        for target in downstream[node]:
            indegree[target] -= 1
            if indegree[target] == 0:
                heapq.heappush(ready, target)
    if len(order) != len(depends_on):
        raise ValueError("graph contains a cycle")
    return order


def layers(depends_on: Adjacency) -> list[list[str]]:
    """Group nodes into dependency levels.

    Every node in layer *n* depends only on nodes in layers ``< n``, so each
    layer is a set the engine may run **concurrently**.  This is what makes
    parallel execution fall out of the plan shape rather than out of explicit
    fan-out syntax, and it is also the row assignment used by the renderer's
    layout.
    """
    order = topological_order(depends_on)
    depth: dict[str, int] = {}
    for node in order:
        dependencies = [d for d in depends_on.get(node, ()) if d in depth]
        depth[node] = 1 + max((depth[d] for d in dependencies), default=-1)
    grouped: dict[int, list[str]] = {}
    for node, level in depth.items():
        grouped.setdefault(level, []).append(node)
    return [sorted(grouped[level]) for level in sorted(grouped)]


def descendants(depends_on: Adjacency, node: str) -> set[str]:
    """Return every node transitively downstream of ``node`` (excluding it)."""
    downstream: dict[str, list[str]] = {key: [] for key in depends_on}
    for target, dependencies in depends_on.items():
        for dependency in dependencies:
            if dependency in downstream:
                downstream[dependency].append(target)
    seen: set[str] = set()
    queue = deque(downstream.get(node, ()))
    while queue:
        current = queue.popleft()
        if current in seen:
            continue
        seen.add(current)
        queue.extend(downstream.get(current, ()))
    seen.discard(node)
    return seen


def ancestors(depends_on: Adjacency, node: str) -> set[str]:
    """Return every node transitively upstream of ``node`` (excluding it)."""
    seen: set[str] = set()
    queue = deque(depends_on.get(node, ()))
    while queue:
        current = queue.popleft()
        if current in seen or current not in depends_on:
            continue
        seen.add(current)
        queue.extend(depends_on.get(current, ()))
    seen.discard(node)
    return seen


def roots(depends_on: Adjacency) -> list[str]:
    """Nodes with no dependencies — the engine's initial ready set."""
    return sorted(node for node, deps in depends_on.items() if not deps)


def leaves(depends_on: Adjacency) -> list[str]:
    """Nodes nothing depends on — typically the plan's deliverables."""
    referenced: set[str] = set()
    for dependencies in depends_on.values():
        referenced.update(dependencies)
    return sorted(node for node in depends_on if node not in referenced)


def critical_path(depends_on: Adjacency, weights: Mapping[str, float] | None = None) -> list[str]:
    """Longest weighted chain through the DAG.

    With no ``weights`` every node costs 1, so this is the longest dependency
    chain — the lower bound on wall-clock time when everything else runs in
    parallel.  Feed measured durations back in to find the real bottleneck.
    """
    order = topological_order(depends_on)
    cost = weights or {}
    best: dict[str, float] = {}
    previous: dict[str, str | None] = {}
    for node in order:
        options = [
            (best[d], d) for d in depends_on.get(node, ()) if d in best
        ]
        base, parent = max(options, default=(0.0, None))
        best[node] = base + float(cost.get(node, 1.0))
        previous[node] = parent
    if not best:
        return []
    end = max(best, key=lambda node: (best[node], node))
    chain: list[str] = []
    walker: str | None = end
    while walker is not None:
        chain.append(walker)
        walker = previous.get(walker)
    chain.reverse()
    return chain


def to_dot(depends_on: Adjacency, *, labels: Mapping[str, str] | None = None) -> str:
    """Render the graph as Graphviz DOT — handy for debugging and docs."""
    text = labels or {}
    lines = ["digraph plan {", '  rankdir="TB";', '  node [shape="box"];']
    for node in sorted(depends_on):
        label = text.get(node, node).replace('"', '\\"')
        lines.append(f'  "{node}" [label="{label}"];')
    for node in sorted(depends_on):
        for dependency in depends_on[node]:
            lines.append(f'  "{dependency}" -> "{node}";')
    lines.append("}")
    return "\n".join(lines)


def adjacency_from(nodes: Iterable[Mapping[str, object]], *, id_key: str = "id", deps_key: str = "depends_on") -> dict[str, list[str]]:
    """Build an adjacency map from a sequence of dict-like nodes."""
    result: dict[str, list[str]] = {}
    for node in nodes:
        node_id = str(node[id_key])
        raw = node.get(deps_key) or []
        result[node_id] = [str(item) for item in raw]  # type: ignore[union-attr]
    return result


__all__ = [
    "Adjacency",
    "GraphIssue",
    "validate",
    "is_dag",
    "find_cycle",
    "topological_order",
    "layers",
    "descendants",
    "ancestors",
    "roots",
    "leaves",
    "critical_path",
    "to_dot",
    "adjacency_from",
]
