from __future__ import annotations

import io
import json

from typing import TYPE_CHECKING
from typing import Any
from typing import cast

import pytest

from cleo.io.buffered_io import BufferedIO
from cleo.io.null_io import NullIO
from cleo.io.outputs.output import Verbosity
from poetry.core.packages.project_package import ProjectPackage

from poetry.factory import Factory
from poetry.mixology.failure import SolveFailureError
from poetry.mixology.trace import SolverTracer
from poetry.mixology.version_solver import VersionSolver
from poetry.puzzle.provider import Provider
from poetry.repositories import Repository
from poetry.repositories import RepositoryPool
from tests.mixology.helpers import add_to_repo


if TYPE_CHECKING:
    from cleo.io.io import IO


def make_provider(
    root: ProjectPackage, repo: Repository, io: IO | None = None
) -> Provider:
    pool = RepositoryPool()
    pool.add_repository(repo)
    return Provider(root, pool, io or NullIO())


def run_solver(
    root: ProjectPackage,
    provider: Provider,
    tracer: SolverTracer | None = None,
) -> dict[str, str]:
    solver = VersionSolver(root, provider, tracer)
    with provider.use_latest_for([]):
        result = solver.solve()
    return {package.name: str(package.version) for package in result.packages}


@pytest.fixture
def tracer() -> SolverTracer:
    return SolverTracer(io.StringIO(), collect=True)


def trace_events(tracer: SolverTracer) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", tracer.events)


def test_successful_resolution_records_ordered_events(
    root: ProjectPackage, tracer: SolverTracer
) -> None:
    root.add_dependency(Factory.create_dependency("a", "*"))
    repo = Repository("repo")
    add_to_repo(repo, "a", "1.0.0", deps={"b": "^1.0"})
    add_to_repo(repo, "b", "1.0.0")
    provider = make_provider(root, repo)

    result = run_solver(root, provider, tracer)

    events = trace_events(tracer)
    assert events[0]["event"] == "run-start"
    assert events[-1]["event"] == "run-end"
    assert events[-1]["outcome"] == "success"
    assert [event["seq"] for event in events] == list(range(len(events)))
    assert {event["run"] for event in events} == {0}

    assert result == {"a": "1.0.0", "b": "1.0.0"}
    decisions = [
        event
        for event in events
        if event["event"] == "decision" and event["package"]["name"] != "myapp"
    ]
    assert {
        (event["package"]["name"], event["package"]["version"]) for event in decisions
    } == {("a", "1.0.0"), ("b", "1.0.0")}
    for event in decisions:
        assert event["preference"] in {
            "DIRECT_ORIGIN",
            "NO_CHOICE",
            "USE_LATEST",
            "LOCKED",
            "DEFAULT",
        }
        assert event["locked"] is False
        assert isinstance(event["candidate_count"], int)
        assert event["candidate_count"] >= 1
    run_end = events[-1]
    assert run_end["attempted_solutions"] == 1
    assert {
        (package["name"], package["version"]) for package in run_end["decisions"]
    } == {("a", "1.0.0"), ("b", "1.0.0")}

    # Propagation conclusions carry the assignment index within the run.
    derived_events = [event for event in events if event["event"] == "derived"]
    assert derived_events
    for event in derived_events:
        assert isinstance(event["index"], int)
        assert event["index"] >= 0
        assert event["cause_id"].startswith("inc-")

    # A dependency-cause fact carries the constraint source:
    # a 1.0.0 (positive) depends on b ^1.0 (negative).
    dep_fact = next(
        event
        for event in events
        if event["event"] == "fact"
        and event["incompatibility"]["cause"] == "dependency"
        and any(
            term["name"] == "b" and term["constraint"] == "^1.0"
            for term in event["incompatibility"]["terms"]
        )
    )
    terms = dep_fact["incompatibility"]["terms"]
    assert any(
        term["name"] == "a" and term["constraint"] == "1.0.0" and term["positive"]
        for term in terms
    )
    assert any(term["name"] == "b" and not term["positive"] for term in terms)


def test_backtrack_is_recorded_with_levels_and_undo(
    root: ProjectPackage, tracer: SolverTracer
) -> None:
    root.add_dependency(Factory.create_dependency("a", ">=1.0.0"))
    repo = Repository("repo")
    add_to_repo(repo, "a", "1.0.0")
    add_to_repo(repo, "a", "2.0.0", deps={"b": "1.0.0"})
    add_to_repo(repo, "b", "1.0.0", deps={"a": "1.0.0"})

    result = run_solver(root, make_provider(root, repo), tracer)
    # b is only reachable through a 2.0.0, which is rejected, so the final
    # solution keeps a 1.0.0 only.
    assert result == {"a": "1.0.0"}

    events = trace_events(tracer)
    kinds = [event["event"] for event in events]
    first_conflict = kinds.index("conflict")
    assert first_conflict < kinds.index("conflict-resolution")
    assert "conflict-resolution" in kinds[first_conflict:]

    # b 1.0.0 is rejected as a candidate first (it requires a 1.0.0 while
    # a 2.0.0 is selected), so the backjump undoes the a 2.0.0 decision.
    rejected_b = next(
        event
        for event in events
        if event["event"] == "candidate-rejected" and event["package"]["name"] == "b"
    )
    assert rejected_b["package"]["version"] == "1.0.0"
    assert rejected_b["seq"] < first_conflict

    backtrack = next(event for event in events if event["event"] == "backtrack")
    assert kinds.index("conflict-resolution") < kinds.index("backtrack")
    assert backtrack["from_level"] == 2
    assert backtrack["to_level"] == 1
    assert backtrack["undone_decisions"] == ["a"]

    # A new decision is made after the backtrack.
    assert any(
        event["event"] == "decision"
        and event["package"]["name"] == "a"
        and event["package"]["version"] == "1.0.0"
        and event["seq"] > backtrack["seq"]
        for event in events
    )
    assert events[-1]["attempted_solutions"] == 2

    # Every incompatibility id referenced anywhere exists in the trace.
    known_ids = {
        node_id for event in events for node_id in _all_incompatibility_ids(event)
    }
    for referenced in _all_incompatibility_references(events):
        assert referenced in known_ids


def test_candidate_rejected_is_recorded_but_not_decided(
    root: ProjectPackage, tracer: SolverTracer
) -> None:
    root.add_dependency(Factory.create_dependency("a", "*"))
    root.add_dependency(Factory.create_dependency("c", "1.0.0"))
    repo = Repository("repo")
    add_to_repo(repo, "c", "1.0.0")
    add_to_repo(repo, "c", "2.0.0")
    add_to_repo(repo, "a", "1.0.0")
    add_to_repo(repo, "a", "2.0.0", deps={"c": "^2.0"})

    result = run_solver(root, make_provider(root, repo), tracer)
    assert result == {"a": "1.0.0", "c": "1.0.0"}

    rejected = next(
        event
        for event in trace_events(tracer)
        if event["event"] == "candidate-rejected"
    )
    assert rejected["package"]["name"] == "a"
    assert rejected["package"]["version"] == "2.0.0"
    assert rejected["conflicts"]
    assert any(
        term["name"] == "c" and not term["positive"]
        for incompatibility in rejected["conflicts"]
        for term in incompatibility["terms"]
    )
    decided_a = [
        event
        for event in trace_events(tracer)
        if event["event"] == "decision" and event["package"]["name"] == "a"
    ]
    assert [event["package"]["version"] for event in decided_a] == ["1.0.0"]


def test_no_versions_is_recorded_before_failure(
    root: ProjectPackage, tracer: SolverTracer
) -> None:
    root.add_dependency(Factory.create_dependency("a", ">=99"))
    repo = Repository("repo")

    with pytest.raises(SolveFailureError):
        run_solver(root, make_provider(root, repo), tracer)

    events = trace_events(tracer)
    no_versions = next(event for event in events if event["event"] == "no-versions")
    assert no_versions["dependency"]["name"] == "a"
    assert no_versions["dependency"]["constraint"] == ">=99"
    failure = next(event for event in events if event["event"] == "failure")
    assert no_versions["seq"] < failure["seq"]
    assert events[-1]["event"] == "run-end"
    assert events[-1]["outcome"] == "failure"


def test_failure_trace_locates_external_constraint_sources(
    root: ProjectPackage, tracer: SolverTracer
) -> None:
    root.add_dependency(Factory.create_dependency("a", "*"))
    repo = Repository("repo")
    add_to_repo(repo, "a", "1.0.0", deps={"b": "^2.0.0"})
    add_to_repo(repo, "b", "1.0.0")

    with pytest.raises(SolveFailureError):
        run_solver(root, make_provider(root, repo), tracer)

    events = trace_events(tracer)
    failure = next(event for event in events if event["event"] == "failure")
    assert failure["graph"]
    assert failure["external"]

    fact_ids_before_failure = {
        event["incompatibility"]["id"]
        for event in events
        if event["event"] == "fact" and event["seq"] < failure["seq"]
    }
    assert set(failure["external"]) <= fact_ids_before_failure

    # The conflicting external requirement (a 1.0.0 needs b ^2.0.0) is
    # recoverable from the graph nodes alone.
    external_nodes = [failure["graph"][node_id] for node_id in failure["external"]]
    assert any(
        term["name"] == "b" and term["constraint"] == "^2.0.0"
        for node in external_nodes
        for term in node["terms"]
    )
    assert any(
        term["name"] == "a" and term["constraint"] == "1.0.0" and term["positive"]
        for node in external_nodes
        for term in node["terms"]
    )


def _build_simple_fixture(latest: str | None) -> tuple[ProjectPackage, Repository]:
    root = ProjectPackage("myapp", "0.0.0")
    root.add_dependency(Factory.create_dependency("a", "*"))
    repo = Repository("repo")
    add_to_repo(repo, "a", "1.0.0")
    if latest is not None:
        add_to_repo(repo, "a", latest)
    return root, repo


def test_identical_runs_produce_byte_identical_traces() -> None:
    outputs = []
    for _ in range(2):
        root, repo = _build_simple_fixture("2.0.0")
        sink = io.StringIO()
        tracer = SolverTracer(sink)
        run_solver(root, make_provider(root, repo), tracer)
        outputs.append(sink.getvalue())

    assert outputs[0] == outputs[1]
    for line in outputs[0].splitlines():
        json.loads(line)


def test_first_diverging_event_explains_drift() -> None:
    sinks: list[list[dict[str, Any]]] = []
    for latest in (None, "2.0.0"):
        root, repo = _build_simple_fixture(latest)
        tracer = SolverTracer(io.StringIO(), collect=True)
        run_solver(root, make_provider(root, repo), tracer)
        sinks.append(trace_events(tracer))

    older, newer = sinks
    divergence = next(
        index for index, (left, right) in enumerate(zip(older, newer)) if left != right
    )
    assert older[:divergence] == newer[:divergence]
    assert newer[divergence]["event"] == "decision"
    assert newer[divergence]["package"]["name"] == "a"
    assert newer[divergence]["package"]["version"] == "2.0.0"
    assert older[divergence]["package"]["version"] == "1.0.0"


def test_tracing_does_not_change_result_or_debug_output(
    root: ProjectPackage,
) -> None:
    def build() -> tuple[ProjectPackage, Repository]:
        fixture_root = ProjectPackage("myapp", "0.0.0")
        fixture_root.add_dependency(Factory.create_dependency("a", ">=1.0.0"))
        repo = Repository("repo")
        add_to_repo(repo, "a", "1.0.0")
        add_to_repo(repo, "a", "2.0.0", deps={"b": "1.0.0"})
        add_to_repo(repo, "b", "1.0.0", deps={"a": "1.0.0"})
        return fixture_root, repo

    def stable_debug_output(text: str) -> list[str]:
        return [
            line for line in text.splitlines() if "Version solving took" not in line
        ]

    root_without_tracer, repo = build()
    io_without = BufferedIO()
    io_without.set_verbosity(Verbosity.DEBUG)
    result_without = run_solver(
        root_without_tracer, make_provider(root_without_tracer, repo, io_without)
    )
    output_without = stable_debug_output(io_without.fetch_output())

    root_with_tracer, _ = build()
    io_with = BufferedIO()
    io_with.set_verbosity(Verbosity.DEBUG)
    tracer = SolverTracer(io.StringIO())
    result_with = run_solver(
        root_with_tracer, make_provider(root_with_tracer, repo, io_with), tracer
    )
    output_with = stable_debug_output(io_with.fetch_output())

    assert result_without == result_with == {"a": "1.0.0"}
    assert output_without == output_with


def _all_incompatibility_ids(event: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    if "incompatibility" in event:
        ids.add(str(event["incompatibility"]["id"]))
    if "graph" in event:
        ids.update(str(key) for key in event["graph"])
    if "conflicts" in event:
        for incompatibility in event["conflicts"]:
            ids.add(str(incompatibility["id"]))
    return ids


def _all_incompatibility_references(events: list[dict[str, Any]]) -> set[str]:
    refs: set[str] = set()
    for event in events:
        refs.update(_all_incompatibility_ids(event))
        if "cause_id" in event:
            refs.add(str(event["cause_id"]))
        satisfier = event.get("satisfier")
        if isinstance(satisfier, dict) and "cause_id" in satisfier:
            refs.add(str(satisfier["cause_id"]))
        incompatibility = event.get("incompatibility")
        if isinstance(incompatibility, dict):
            derived_from = incompatibility.get("derived_from")
            if isinstance(derived_from, dict):
                refs.add(str(derived_from["conflict"]))
                refs.add(str(derived_from["other"]))
        graph = event.get("graph")
        if isinstance(graph, dict):
            for node in graph.values():
                derived_from = node.get("derived_from")
                if isinstance(derived_from, dict):
                    refs.add(str(derived_from["conflict"]))
                    refs.add(str(derived_from["other"]))
    return refs
