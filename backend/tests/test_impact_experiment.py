"""Independent expectations for the isolated membership experiment, not EFQA T/C acceptance."""

import json
from dataclasses import replace
from itertools import permutations, product
from pathlib import Path

import pytest

from scripts.impact_experiment import (
    Change,
    Completeness,
    Membership,
    Snapshot,
    TraversalLimitExceeded,
    assess,
    load_case,
)

INPUTS = json.loads(
    (Path(__file__).parents[1] / "scripts" / "impact_inputs.json").read_text(encoding="utf-8")
)
EXPECTED = json.loads(Path(__file__).with_name("impact_expected.json").read_text(encoding="utf-8"))


def items_in(result, classification):
    return {
        entry.item_id for entry in result.explanations if entry.classification == classification
    }


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_independent_fixture_expectations(name):
    snapshot, change = load_case(INPUTS, name)
    result = assess(snapshot, change)
    expected = EXPECTED[name]
    assert result.coverage == expected["coverage"]
    assert list(result.issues) == expected["issues"]
    for classification in (
        "requires_assessment",
        "possibly_affected",
        "excluded_under_assumptions",
    ):
        assert items_in(result, classification) == set(expected[classification])
    entries = {entry.item_id: entry for entry in result.explanations}
    assert len(entries) == len(result.explanations) == len(snapshot.items)
    for item, path in expected["paths"].items():
        assert list(entries[item].path) == path
    for entry in result.explanations:
        if entry.classification == "excluded_under_assumptions":
            assert entry.completeness_reference == snapshot.completeness.reference
        else:
            assert entry.path[0] == change.evidence_id
            assert entry.path[-1] == entry.item_id


def full_scan_reachable(edges, seeds):
    """Repeated full relation scan, without indexes, BFS or production helpers."""
    reached = set(seeds)
    while True:
        expanded = reached | {parent for child, parent in edges if child in reached}
        if expanded == reached:
            return reached
        reached = expanded


def test_all_three_item_graphs_against_full_scan_oracle():
    items = ("a", "b", "c")
    possible_edges = tuple(permutations(items, 2))
    checked = 0
    for flags in product((False, True), repeat=len(possible_edges)):
        edges = tuple(edge for edge, present in zip(possible_edges, flags, strict=True) if present)
        snapshot = Snapshot(
            "G",
            items,
            tuple(Membership(f"M{i}", *edge) for i, edge in enumerate(edges)),
            Completeness("proof-G", "G"),
        )
        # Detect a cycle independently: some edge leads back to its own child.
        cyclic = any(child in full_scan_reachable(edges, (parent,)) for child, parent in edges)
        conflicting = any(sum(child == item for child, _ in edges) > 1 for item in items)
        scope_open = cyclic or conflicting
        for seed in items:
            reached = full_scan_reachable(edges, (seed,))
            for candidate in (False, True):
                change = Change(
                    "R",
                    reliable_items=() if candidate else (seed,),
                    candidate_items=(seed,) if candidate else (),
                )
                result = assess(snapshot, change)
                definite = set() if candidate else reached
                possible = set(items) - definite if scope_open else reached - definite
                assert items_in(result, "requires_assessment") == definite
                assert items_in(result, "possibly_affected") == possible
                assert items_in(result, "excluded_under_assumptions") == (
                    set(items) - definite - possible
                )
                assert result.coverage == (
                    "open" if scope_open else "bounded_candidates" if candidate else "complete"
                )
                assertions = {edge.assertion_id: edge for edge in snapshot.memberships}
                for entry in result.explanations:
                    if entry.reason not in {
                        "identified_membership_path",
                        "candidate_membership_path",
                    }:
                        continue
                    assert entry.path[:2] == ("R", seed)
                    assert entry.path[-1] == entry.item_id
                    for offset in range(2, len(entry.path), 2):
                        edge = assertions[entry.path[offset]]
                        assert (edge.component_id, edge.assembly_id) == (
                            entry.path[offset - 1],
                            entry.path[offset + 1],
                        )
                checked += 1
    assert checked == 384  # 64 graphs x 3 seed items x 2 identification modes.


def test_paths_are_stable_under_edge_catalog_and_candidate_permutations():
    snapshot, _ = load_case(INPUTS, "nested-assembly")
    change = Change("U1", candidate_items=("insert-001", "insert-002"))
    expected = assess(snapshot, change)
    for edges in permutations(snapshot.memberships):
        result = assess(
            replace(snapshot, memberships=edges, items=tuple(reversed(snapshot.items))),
            replace(change, candidate_items=tuple(reversed(change.candidate_items))),
        )
        assert result == expected


def test_new_membership_exposes_retained_evidence_without_new_evidence_identity():
    before, retained_evidence = load_case(INPUTS, "before-membership")
    after, _ = load_case(INPUTS, "late-component")
    first = assess(before, retained_evidence)
    second = assess(after, retained_evidence)
    assert first.evidence_id == second.evidence_id == "RC"
    assert "assembly-001" not in items_in(first, "requires_assessment")
    assert "assembly-001" in items_in(first, "possibly_affected")
    assert "assembly-001" in items_in(second, "requires_assessment")
    # Re-querying a pure model is not an automated ingestion trigger or a new raw write.
    assert first == assess(before, retained_evidence)


def test_loss_of_completeness_never_creates_exclusions():
    snapshot, change = load_case(INPUTS, "late-component")
    complete = assess(snapshot, change)
    unknown = assess(replace(snapshot, completeness=None), change)
    assert items_in(unknown, "requires_assessment") == items_in(complete, "requires_assessment")
    assert not items_in(unknown, "excluded_under_assumptions")
    assert items_in(complete, "excluded_under_assumptions") <= items_in(
        unknown, "possibly_affected"
    )


def test_unknown_scope_applies_to_new_catalog_item_when_reassessed():
    snapshot, change = load_case(INPUTS, "unknown-scope")
    new = replace(snapshot, snapshot_id="G2", items=(*snapshot.items, "assembly-004"))
    new = replace(new, completeness=Completeness("proof-G2", "G2"))
    result = assess(new, change)
    assert "assembly-004" in items_in(result, "possibly_affected")
    assert not items_in(result, "excluded_under_assumptions")


def test_adding_historical_membership_never_shrinks_reachability():
    snapshot, change = load_case(INPUTS, "late-component")
    larger = replace(
        snapshot,
        snapshot_id="G2",
        completeness=Completeness("proof-G2", "G2"),
        memberships=(
            *snapshot.memberships,
            Membership("M3", "assembly-001", "module-001", "historical"),
        ),
    )
    assert items_in(assess(snapshot, change), "requires_assessment") < items_in(
        assess(larger, change), "requires_assessment"
    )


@pytest.mark.parametrize(
    "kwargs", [{"max_items": 1}, {"max_memberships": 1}, {"max_path_steps": 1}]
)
def test_budget_exhaustion_raises_without_a_partial_result(kwargs):
    snapshot, change = load_case(INPUTS, "nested-assembly")
    with pytest.raises(TraversalLimitExceeded, match="no result published"):
        assess(snapshot, change, **kwargs)


def test_self_reference_is_conflict_but_historical_self_reference_terminates():
    snapshot = Snapshot(
        "G", ("a", "b"), (Membership("M1", "a", "a"),), Completeness("proof-G", "G")
    )
    result = assess(snapshot, Change("R", ("a",)))
    assert result.coverage == "open"
    assert "current_membership_cycle" in result.issues
    historical = replace(snapshot, memberships=(Membership("M1", "a", "a", "historical"),))
    assert assess(historical, Change("R", ("a",))).coverage == "complete"


def test_stale_completeness_and_unknown_catalog_references_are_rejected():
    snapshot, change = load_case(INPUTS, "late-component")
    with pytest.raises(ValueError, match="exact snapshot"):
        assess(replace(snapshot, snapshot_id="G2"), change)
    with pytest.raises(ValueError, match="outside"):
        assess(snapshot, Change("R", ("not-in-catalog",)))
    with pytest.raises(ValueError, match="empty seed"):
        assess(snapshot, Change("R"))


def test_duplicate_assertion_id_cannot_hide_another_membership():
    snapshot, change = load_case(INPUTS, "late-component")
    duplicate = Membership("M1", "insert-001", "assembly-003")
    with pytest.raises(ValueError, match="Duplicate membership"):
        assess(replace(snapshot, memberships=(*snapshot.memberships, duplicate)), change)


def test_two_current_parents_are_a_conflict_not_a_complete_tree():
    snapshot, change = load_case(INPUTS, "removed-component")
    simultaneous = replace(
        snapshot,
        memberships=tuple(replace(edge, state="current") for edge in snapshot.memberships),
    )
    result = assess(simultaneous, change)
    assert result.coverage == "open"
    assert result.issues == ("conflicting_current_memberships",)
    assert not items_in(result, "excluded_under_assumptions")


def test_long_history_is_iterative_and_explanation_budget_is_enforced():
    items = tuple(f"item-{index}" for index in range(1200))
    snapshot = Snapshot(
        "long",
        items,
        tuple(Membership(f"M{i}", items[i], items[i + 1]) for i in range(len(items) - 1)),
        Completeness("proof-long", "long"),
    )
    with pytest.raises(TraversalLimitExceeded, match="Explanation budget"):
        assess(snapshot, Change("R", (items[0],)), max_path_steps=100)
