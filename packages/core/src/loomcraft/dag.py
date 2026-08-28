"""Compatibility contract for node/edge-shaped DAG definitions.

The AI-native Plan contract uses ``steps`` and ``depends_on`` because it is
compact for tool calls. Some hosts already have a visual workflow format with
``nodes`` and ``edges``. This module validates that format and provides a
loss-aware conversion to Plan; domain-specific node types remain host handlers.
"""

from __future__ import annotations

import copy
import math
import re
from collections import deque
from typing import Any, Dict, List, Mapping, Set

from .plan import FAILURE_POLICIES, RetryPolicy


NODE_TYPES = (
    "input.upload",
    "data.validate",
    "data.transform",
    "data.profile",
    "agent.task",
    "tool.external",
    "script.python",
    "script.shell",
    "human.approval",
    "report.generate",
    "artifact.download",
    "control.branch",
    "control.join",
)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class DAGValidationError(ValueError):
    pass


def _object(value: Any, label: str) -> Dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DAGValidationError("%s must be an object" % label)
    return dict(value)


def validate_dag(raw: Any) -> Dict[str, Any]:
    value = _object(raw, "dag")
    allowed = {"id", "name", "version", "description", "nodes", "edges", "metadata"}
    unknown = set(value) - allowed
    if unknown:
        raise DAGValidationError("dag contains unsupported fields: %s" % ", ".join(sorted((str(item) for item in unknown))))
    identifier = value.get("id")
    name = value.get("name")
    version = value.get("version")
    if not isinstance(identifier, str) or _ID_RE.fullmatch(identifier) is None:
        raise DAGValidationError("dag.id has an invalid format")
    if not isinstance(name, str) or not name.strip():
        raise DAGValidationError("dag.name is required")
    if not isinstance(version, str) or not version.strip():
        raise DAGValidationError("dag.version is required")
    raw_nodes = value.get("nodes")
    raw_edges = value.get("edges")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise DAGValidationError("dag.nodes must contain at least one node")
    if not isinstance(raw_edges, list):
        raise DAGValidationError("dag.edges must be a list")
    nodes: List[Dict[str, Any]] = []
    ids: Set[str] = set()
    for raw_node in raw_nodes:
        node = _object(raw_node, "node")
        allowed_node = {"id", "name", "type", "description", "inputs", "outputs", "config", "agent", "approval", "retry", "timeout_seconds", "on_failure", "metadata"}
        unknown_node = set(node) - allowed_node
        if unknown_node:
            raise DAGValidationError("node contains unsupported fields: %s" % ", ".join(sorted((str(item) for item in unknown_node))))
        node_id = node.get("id")
        if not isinstance(node_id, str) or _ID_RE.fullmatch(node_id) is None:
            raise DAGValidationError("node.id has an invalid format")
        if node_id in ids:
            raise DAGValidationError("node ids must be unique")
        ids.add(node_id)
        node_type = node.get("type")
        if node_type not in NODE_TYPES:
            raise DAGValidationError("node %r has an unsupported type" % node_id)
        if not isinstance(node.get("name"), str) or not node["name"].strip():
            raise DAGValidationError("node %r name is required" % node_id)
        for field in ("inputs", "outputs"):
            if node.get(field) is not None and not isinstance(node.get(field), list):
                raise DAGValidationError("node %r %s must be a list" % (node_id, field))
            port_names = set()
            for port in node.get(field, []) or []:
                if not isinstance(port, Mapping) or not isinstance(port.get("name"), str) or not isinstance(port.get("artifact_type"), str):
                    raise DAGValidationError("node %r %s ports require name and artifact_type" % (node_id, field))
                if "required" in port and not isinstance(port.get("required"), bool):
                    raise DAGValidationError("node %r %s port required must be boolean" % (node_id, field))
                if port["name"] in port_names:
                    raise DAGValidationError("node %r has duplicate %s port names" % (node_id, field))
                port_names.add(port["name"])
        if node.get("config") is not None and not isinstance(node.get("config"), Mapping):
            raise DAGValidationError("node %r config must be an object" % node_id)
        for optional_object in ("agent", "approval", "metadata"):
            if node.get(optional_object) is not None and not isinstance(node.get(optional_object), Mapping):
                raise DAGValidationError("node %r %s must be an object" % (node_id, optional_object))
        if node.get("on_failure", "stop") not in FAILURE_POLICIES:
            raise DAGValidationError("node %r on_failure is invalid" % node_id)
        if node.get("retry") is not None:
            try:
                retry_value = dict(node.get("retry")) if isinstance(node.get("retry"), Mapping) else node.get("retry")
                # The legacy DAG schema treated max_attempts=0 as “no retry”;
                # normalize that spelling to one total attempt on conversion.
                if isinstance(retry_value, dict) and retry_value.get("max_attempts") == 0:
                    retry_value["max_attempts"] = 1
                RetryPolicy.from_raw(retry_value)
            except ValueError as exc:
                raise DAGValidationError("node %r retry is invalid" % node_id) from exc
        timeout = node.get("timeout_seconds")
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(float(timeout)) or timeout <= 0):
            raise DAGValidationError("node %r timeout_seconds is invalid" % node_id)
        nodes.append({
            "id": node_id,
            "name": node["name"].strip(),
            "type": node_type,
            "description": str(node.get("description", ""))[:1000],
            "inputs": copy.deepcopy(node.get("inputs", [])),
            "outputs": copy.deepcopy(node.get("outputs", [])),
            "config": copy.deepcopy(dict(node.get("config", {}))),
            "agent": copy.deepcopy(node.get("agent")),
            "approval": copy.deepcopy(node.get("approval")),
            "retry": copy.deepcopy(node.get("retry")),
            "timeout_seconds": node.get("timeout_seconds"),
            "on_failure": node.get("on_failure", "stop"),
            "metadata": copy.deepcopy(dict(node.get("metadata", {}))) if isinstance(node.get("metadata", {}), Mapping) else {},
        })
    edges: List[Dict[str, Any]] = []
    seen_edges: Set[str] = set()
    indegree = {identifier: 0 for identifier in ids}
    downstream = {identifier: [] for identifier in ids}
    for raw_edge in raw_edges:
        edge = _object(raw_edge, "edge")
        if set(edge) - {"from", "to", "condition"}:
            raise DAGValidationError("edge contains unsupported fields")
        source, target = edge.get("from"), edge.get("to")
        if source not in ids or target not in ids:
            raise DAGValidationError("edge references an unknown node")
        if source == target:
            raise DAGValidationError("a node cannot depend on itself")
        if edge.get("condition") is not None and not isinstance(edge.get("condition"), str):
            raise DAGValidationError("edge condition must be a string")
        key = "%s->%s" % (source, target)
        if key in seen_edges:
            raise DAGValidationError("duplicate edge %s" % key)
        seen_edges.add(key)
        indegree[target] += 1
        downstream[source].append(target)
        edges.append({"from": source, "to": target, **({"condition": edge["condition"]} if edge.get("condition") is not None else {})})
    queue = deque(sorted(identifier for identifier, degree in indegree.items() if degree == 0))
    order: List[str] = []
    while queue:
        identifier = queue.popleft()
        order.append(identifier)
        for target in sorted(downstream[identifier]):
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if len(order) != len(ids):
        raise DAGValidationError("dag dependencies must be acyclic")
    return {
        "id": value["id"],
        "name": value["name"].strip(),
        "version": value["version"].strip(),
        "description": str(value.get("description", ""))[:2000],
        "nodes": nodes,
        "edges": edges,
        "metadata": copy.deepcopy(dict(value.get("metadata", {}))) if isinstance(value.get("metadata", {}), Mapping) else {},
        "topological_order": order,
    }


def plan_from_dag(raw: Any, revision: int = 1) -> Dict[str, Any]:
    """Convert a validated node/edge DAG into the AI-native Plan shape."""
    dag = validate_dag(raw)
    dependencies: Dict[str, List[str]] = {node["id"]: [] for node in dag["nodes"]}
    for edge in dag["edges"]:
        dependencies[edge["to"]].append(edge["from"])
    steps: List[Dict[str, Any]] = []
    for node in dag["nodes"]:
        node_type = node["type"]
        if node_type == "human.approval":
            kind = "review"
        elif node_type in {"agent.task", "script.python", "script.shell", "data.validate", "data.transform", "data.profile", "control.branch", "control.join", "input.upload", "artifact.download"}:
            kind = "dynamic"
        elif node_type in {"tool.external", "report.generate"} and node["config"].get("capability"):
            kind = "capability" if node_type == "tool.external" else "workflow"
        else:
            kind = "dynamic"
        step: Dict[str, Any] = {
            "id": node["id"],
            "title": node["name"],
            "kind": kind,
            "depends_on": dependencies[node["id"]],
            "description": node["description"],
            "metadata": {**node.get("metadata", {}), "source_node_type": node_type},
        }
        if kind in {"capability", "workflow"}:
            step["capability"] = node["config"]["capability"]
        if node.get("retry") is not None:
            retry = copy.deepcopy(node["retry"])
            if isinstance(retry, Mapping) and retry.get("max_attempts") == 0:
                retry["max_attempts"] = 1
            step["retry"] = retry
        if node.get("timeout_seconds") is not None:
            step["timeout_seconds"] = node["timeout_seconds"]
        if node.get("on_failure") is not None:
            step["on_failure"] = node["on_failure"]
        steps.append(step)
    return {"goal": dag["name"], "summary": dag.get("description", ""), "revision": revision, "steps": steps, "metadata": {"source_dag_id": dag["id"], "source_dag_version": dag["version"]}}
