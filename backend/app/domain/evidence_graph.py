"""Bounded membership impact used by the unified coordinator and the CLI experiment.

Reachability explains review, not physical causation. An asserted completeness
proof is still a trust boundary; traversal does not verify the real installation.
"""

from collections import defaultdict, deque
from dataclasses import dataclass


@dataclass(frozen=True)
class Membership:
    assertion_id: str
    component_id: str
    assembly_id: str
    state: str = "current"


@dataclass(frozen=True)
class Completeness:
    reference: str
    snapshot_id: str


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    items: tuple[str, ...]
    memberships: tuple[Membership, ...]
    completeness: Completeness | None = None


@dataclass(frozen=True)
class Change:
    evidence_id: str
    reliable_items: tuple[str, ...] = ()
    candidate_items: tuple[str, ...] = ()
    unknown_scope: bool = False


@dataclass(frozen=True)
class Explanation:
    item_id: str
    classification: str
    reason: str
    path: tuple[str, ...]
    completeness_reference: str | None = None


@dataclass(frozen=True)
class Impact:
    snapshot_id: str
    evidence_id: str
    coverage: str
    issues: tuple[str, ...]
    explanations: tuple[Explanation, ...]
    visited_items: int
    examined_memberships: int
    policy: str = "impact-v1-membership-experiment"
    synthetic: bool = True


class TraversalLimitExceeded(RuntimeError):
    """No partial Impact is returned. This does not simulate transaction rollback."""


def _validate(snapshot: Snapshot, change: Change) -> None:
    identifiers = [snapshot.snapshot_id, change.evidence_id, *snapshot.items]
    for edge in snapshot.memberships:
        identifiers.extend((edge.assertion_id, edge.component_id, edge.assembly_id))
        if edge.state not in {"current", "historical", "superseded"}:
            raise ValueError("Unknown membership state.")
    if any(not isinstance(value, str) or not value.strip() for value in identifiers):
        raise ValueError("Identifiers must be non-empty strings.")
    if len(set(snapshot.items)) != len(snapshot.items):
        raise ValueError("Duplicate catalog item.")
    if len({edge.assertion_id for edge in snapshot.memberships}) != len(snapshot.memberships):
        raise ValueError("Duplicate membership assertion ID.")
    items = set(snapshot.items)
    referenced = set(change.reliable_items) | set(change.candidate_items)
    for edge in snapshot.memberships:
        referenced.update((edge.component_id, edge.assembly_id))
    if not referenced <= items:
        raise ValueError("An input references an item outside the snapshot catalog.")
    if not change.reliable_items and not change.candidate_items and not change.unknown_scope:
        raise ValueError("An empty seed list is not proof of no impact.")
    proof = snapshot.completeness
    if proof is not None and (not proof.reference or proof.snapshot_id != snapshot.snapshot_id):
        raise ValueError("Completeness must refer to this exact snapshot.")


def _has_current_cycle(snapshot: Snapshot) -> bool:
    """Kahn's algorithm on current assertions only; historical union may be cyclic."""
    outgoing: dict[str, list[str]] = defaultdict(list)
    indegree = dict.fromkeys(snapshot.items, 0)
    for edge in snapshot.memberships:
        if edge.state == "current":
            outgoing[edge.component_id].append(edge.assembly_id)
            indegree[edge.assembly_id] += 1
    pending = deque(item for item in snapshot.items if indegree[item] == 0)
    removed = 0
    while pending:
        item = pending.popleft()
        removed += 1
        for parent in outgoing[item]:
            indegree[parent] -= 1
            if indegree[parent] == 0:
                pending.append(parent)
    return removed != len(snapshot.items)


def assess(
    snapshot: Snapshot,
    change: Change,
    *,
    max_items: int = 10_000,
    max_memberships: int = 50_000,
    max_path_steps: int = 50_000,
) -> Impact:
    """Explain membership reachability, with conservative catalog-wide fallback.

    Limits bound traversal, not parsing/index construction or a runtime transaction.
    A complete proof is a fixture assumption, not a verified enterprise assertion.
    """
    if max_items < 1 or max_memberships < 1 or max_path_steps < 1:
        raise ValueError("Traversal limits must be positive.")
    _validate(snapshot, change)
    parents: dict[str, list[Membership]] = defaultdict(list)
    current_parents: dict[str, set[str]] = defaultdict(set)
    for edge in sorted(snapshot.memberships, key=lambda entry: entry.assertion_id):
        parents[edge.component_id].append(edge)
        if edge.state == "current":
            current_parents[edge.component_id].add(edge.assembly_id)
    issues = []
    if snapshot.completeness is None:
        issues.append("membership_completeness_unproven")
    if change.unknown_scope:
        issues.append("identity_scope_unknown")
    if _has_current_cycle(snapshot):
        issues.append("current_membership_cycle")
    if any(len(assemblies) > 1 for assemblies in current_parents.values()):
        issues.append("conflicting_current_memberships")

    visited: set[str] = set()
    examined = 0
    path_steps = 0

    def path_step() -> None:
        nonlocal path_steps
        path_steps += 1
        if path_steps > max_path_steps:
            raise TraversalLimitExceeded("Explanation budget exceeded; no result published.")

    def visit(item: str) -> None:
        visited.add(item)
        if len(visited) > max_items:
            raise TraversalLimitExceeded("Item traversal budget exceeded; no result published.")

    def walk(seeds: tuple[str, ...]) -> dict[str, tuple[str, ...]]:
        nonlocal examined
        # Store one predecessor per target, not a quadratic set of full paths.
        predecessors: dict[str, tuple[str, Membership] | None] = {}
        pending: deque[str] = deque()
        for item in sorted(set(seeds)):
            visit(item)
            predecessors[item] = None
            pending.append(item)
        while pending:
            child = pending.popleft()
            for edge in parents[child]:
                examined += 1
                if examined > max_memberships:
                    raise TraversalLimitExceeded(
                        "Membership traversal budget exceeded; no result published."
                    )
                if edge.assembly_id not in predecessors:
                    visit(edge.assembly_id)
                    predecessors[edge.assembly_id] = (child, edge)
                    pending.append(edge.assembly_id)
        paths = {}
        for item in predecessors:
            path_step()
            reverse_path = [item]
            current = item
            while predecessors[current] is not None:
                path_step()
                child, edge = predecessors[current]
                reverse_path.extend((edge.assertion_id, child))
                current = child
            paths[item] = (change.evidence_id, *reversed(reverse_path))
        return paths

    reliable_paths = walk(change.reliable_items)
    candidate_paths = walk(change.candidate_items)
    explanations = []
    proof = snapshot.completeness
    for item in sorted(snapshot.items):
        if item in reliable_paths:
            explanation = Explanation(
                item, "requires_assessment", "identified_membership_path", reliable_paths[item]
            )
        elif item in candidate_paths:
            explanation = Explanation(
                item, "possibly_affected", "candidate_membership_path", candidate_paths[item]
            )
        elif issues:
            visit(item)
            path_step()
            explanation = Explanation(
                item,
                "possibly_affected",
                "unproven_scope_catalog_fallback",
                (change.evidence_id, "scope_unknown", item),
            )
        else:
            explanation = Explanation(
                item,
                "excluded_under_assumptions",
                "no_membership_path_in_complete_snapshot",
                (),
                proof.reference,
            )
        explanations.append(explanation)
    coverage = "open" if issues else "bounded_candidates" if candidate_paths else "complete"
    return Impact(
        snapshot.snapshot_id,
        change.evidence_id,
        coverage,
        tuple(sorted(issues)),
        tuple(explanations),
        len(visited),
        examined,
    )


def load_case(document: dict, name: str) -> tuple[Snapshot, Change]:
    """Read trusted synthetic experiment fixtures, not public source messages."""
    case = document["cases"][name]
    snapshot_id = f"synthetic/{name}"
    proof = Completeness(f"fixture-proof/{name}", snapshot_id) if case["complete"] else None
    snapshot = Snapshot(
        snapshot_id,
        tuple(document["items"]),
        tuple(Membership(*entry) for entry in case["memberships"]),
        proof,
    )
    change = Change(
        case["evidence_id"],
        tuple(case.get("reliable_items", [])),
        tuple(case.get("candidate_items", [])),
        case.get("unknown_scope", False),
    )
    return snapshot, change
