"""[FR-06] Task dependency DAG — Kahn topological sort, cycle extraction,
and chain depth computation.

The three public helpers are the FR-06 surface `spec-coverage-check`
looks up:

* `topo_sort(deps)` — return `(order, remaining)` per Kahn's algorithm.
* `cycle_path(deps, remaining)` — extract one concrete cycle from the
  leftover nodes Kahn could never reach.
* `chain_depths(deps, order)` — longest dependency-chain length
  ending at each node.

`dependency_edges` is a small adapter that drops edges whose target is
no longer in the task set, so the same helpers work whether the
graph comes from a fresh in-memory store or a partially-pruned
`tasks.json` (a dangling id means the referenced task was removed
after the fact — `submit` already rejects unknown dependency ids at
write time, so a dangling edge here is a recovery case, not a
validation case).

The module deliberately holds no I/O and no model imports: the
helpers operate on plain mappings of ids, which keeps the
`pytest-cov` measurement of FR-06 independent of the disk store and
the executor.

Citations:
    SPEC.md §3 FR-06 line 144 — `--after` 建立 `depends_on` 邊.
    SPEC.md §3 FR-06 line 145 — Kahn 拓撲排序.
    SPEC.md §3 FR-06 line 147 — 循環偵測.
    SPEC.md §3 FR-06 line 148 — 相依鏈深度上限.
    SPEC.md §6 line 337 — `cli/commands` module location.
    SPEC.md §7 line 388 — exit 5, stderr 列出循環路徑.
    SPEC.md §7 line 389 — exit 5, stderr
        `dependency chain too deep: <n> > <max>`.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Mapping, Sequence, Set, Tuple


def dependency_edges(
    tasks: Iterable[object],
) -> Dict[str, List[str]]:
    """[FR-06] Map each task id to the ids it depends on.

    Dangling ids (a dependency whose task is gone) are dropped so the
    topological sweep only walks edges that exist in the loaded set;
    `submit` already rejects unknown dependency ids at write time
    (SPEC §7 line 385), so a dangling edge here means the referenced
    task was removed after the fact.

    Accepts both `Task` pydantic models (attribute access) and
    `tasks.json` records (mapping access) so the helper works for the
    in-process store and the raw disk payload interchangeably.

    Args:
        tasks: An iterable of `Task` models or task-shaped mappings.
            Each entry must expose an `id` and a `depends_on`
            collection.

    Returns:
        A `dict` mapping every task id to a list of its prerequisite
        ids (only ids that appear as keys in the result are kept).
    """
    def _task_id(task: object) -> str:
        if isinstance(task, Mapping):
            return str(task["id"])
        return str(getattr(task, "id"))

    def _task_deps(task: object) -> Sequence[str]:
        if isinstance(task, Mapping):
            raw = task.get("depends_on", []) or []
        else:
            raw = getattr(task, "depends_on", None) or []
        return list(raw)

    known: Set[str] = {_task_id(t) for t in tasks}
    edges: Dict[str, List[str]] = {}
    for task in tasks:
        tid = _task_id(task)
        edges[tid] = [d for d in _task_deps(task) if d in known]
    return edges


def topo_sort(
    deps: Mapping[str, Sequence[str]],
) -> Tuple[List[str], List[str]]:
    """[FR-06] Kahn topological sort; return `(order, remaining)`.

    `order` is the ids in dependency-satisfied order. `remaining` holds
    the ids Kahn could never reach — non-empty exactly when the graph
    contains a cycle.

    Args:
        deps: A mapping from task id to the list of ids it depends on.
            Every id that appears in any value must also appear as a
            key.

    Returns:
        A 2-tuple `(order, remaining)`. `order` contains every node
        that Kahn was able to reach (in dependency-satisfied order).
        `remaining` contains the nodes Kahn could not reach; their
        ` → `-joined `cycle_path` names one concrete cycle.

    Citations:
        SPEC.md §3 FR-06 line 145 — Kahn 拓撲排序.
        SPEC.md §3 FR-06 line 147 — 循環偵測.
    """
    indegree: Dict[str, int] = {node: len(prereqs) for node, prereqs in deps.items()}
    dependents: Dict[str, List[str]] = {node: [] for node in deps}
    for node, prereqs in deps.items():
        for prereq in prereqs:
            dependents[prereq].append(node)

    # Sort the initial ready queue so the order is deterministic for
    # tasks that share the same in-degree (e.g. siblings in a
    # diamond). Determinism matters for the acceptance tests that
    # compare a.finished_at < b.finished_at under parallel dispatch.
    ready: List[str] = sorted(node for node, count in indegree.items() if count == 0)
    order: List[str] = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for dependent in dependents[node]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)

    emitted = set(order)
    return order, [node for node in deps if node not in emitted]


def cycle_path(
    deps: Mapping[str, Sequence[str]],
    remaining: Sequence[str],
) -> List[str]:
    """[FR-06] Extract one concrete cycle from Kahn's leftover nodes.

    Every leftover node has at least one prerequisite that is itself
    leftover — that is precisely why its indegree never reached zero —
    so walking prerequisites inside the leftover set always revisits a
    node and closes a cycle. The returned list starts and ends with
    the same node so `" → ".join(path)` renders the closed loop.

    Args:
        deps: The full dependency mapping (the same one passed to
            `topo_sort`).
        remaining: The `remaining` list returned by `topo_sort`. Must
            be non-empty.

    Returns:
        A list `[start, ..., start]` whose ` → `-joined form names one
        concrete cycle. The list always starts and ends with the same
        node so the cycle is visibly closed.

    Citations:
        SPEC.md §7 line 388 — exit 5, stderr 列出循環路徑.
    """
    leftover: Set[str] = set(remaining)
    walked: List[str] = []
    position: Dict[str, int] = {}
    node = remaining[0]
    while node not in position:
        position[node] = len(walked)
        walked.append(node)
        # Every leftover node has at least one prereq that is itself
        # leftover (otherwise its in-degree would have been zero and
        # Kahn would have emitted it), so the `next(...)` call below
        # is guaranteed to find a candidate.
        node = next(prereq for prereq in deps[node] if prereq in leftover)
    return walked[position[node]:] + [node]


def chain_depths(
    deps: Mapping[str, Sequence[str]],
    order: Sequence[str],
) -> Dict[str, int]:
    """[FR-06] Longest dependency-chain length ending at each node.

    Walking `order` (already dependency-satisfied) guarantees every
    prerequisite's depth is known before its dependent is visited.
    The depth at a leaf is 1; the depth at a dependent is
    `1 + max(prereq_depth for prereq in deps[node])`.

    Args:
        deps: A mapping from task id to the list of ids it depends on.
        order: The `order` list returned by `topo_sort` (or any
            dependency-satisfied permutation of the ids).

    Returns:
        A `dict` mapping every id in `order` to the longest
        dependency-chain length ending at that node.

    Citations:
        SPEC.md §3 FR-06 line 148 — 相依鏈深度上限.
    """
    depths: Dict[str, int] = {}
    for node in order:
        depths[node] = 1 + max(
            (depths[prereq] for prereq in deps[node]), default=0
        )
    return depths
