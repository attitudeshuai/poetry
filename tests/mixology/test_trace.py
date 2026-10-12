from __future__ import annotations

import io
import json

from typing import Any
from typing import cast

import pytest

from poetry.core.packages.dependency import Dependency
from poetry.core.packages.package import Package
from poetry.core.packages.project_package import ProjectPackage

from poetry.mixology.incompatibility import Incompatibility
from poetry.mixology.incompatibility_cause import ConflictCauseError
from poetry.mixology.incompatibility_cause import DependencyCauseError
from poetry.mixology.incompatibility_cause import NoVersionsCauseError
from poetry.mixology.incompatibility_cause import PlatformCauseError
from poetry.mixology.incompatibility_cause import PythonCauseError
from poetry.mixology.incompatibility_cause import RootCauseError
from poetry.mixology.term import Term
from poetry.mixology.trace import NULL_TRACER
from poetry.mixology.trace import TRACE_SCHEMA_VERSION
from poetry.mixology.trace import SolverTracer
from poetry.mixology.trace import serialize_dependency


def dependency_incompatibility(depender: str, dependee: str) -> Incompatibility:
    return Incompatibility(
        [
            Term(Dependency(depender, "1.0.0"), True),
            Term(Dependency(dependee, "^1.0.0"), False),
        ],
        DependencyCauseError(),
    )


@pytest.fixture
def tracer() -> SolverTracer:
    return SolverTracer(io.StringIO(), collect=True)


def records(tracer: SolverTracer) -> list[dict[str, Any]]:
    sink = cast("io.StringIO", tracer._sink)
    return [json.loads(line) for line in sink.getvalue().splitlines()]


def trace_events(tracer: SolverTracer) -> list[dict[str, Any]]:
    return cast("list[dict[str, Any]]", tracer.events)


def first_event(tracer: SolverTracer, name: str) -> dict[str, Any]:
    return next(event for event in records(tracer) if event["event"] == name)


def test_serialize_dependency_snapshots_sorted_fields() -> None:
    dep = Dependency(
        "MyPackage",
        "^1.0",
        extras=("zeta", "alpha"),
        groups=("zzz", "main"),
    )

    data = serialize_dependency(dep)

    assert data["name"] == "mypackage"
    assert data["complete_name"] == "mypackage[alpha,zeta]"
    assert data["constraint"] == "^1.0"
    assert data["marker"] == ""
    assert data["extras"] == ["alpha", "zeta"]
    assert data["groups"] == ["main", "zzz"]
    assert data["direct_origin"] is False


def test_serialize_dependency_is_a_point_in_time_snapshot() -> None:
    dep = Dependency("a", "^1.0")
    incompatibility = Incompatibility(
        [Term(dep, True), Term(Dependency("b", "^1.0"), False)],
        DependencyCauseError(),
    )
    tracer = SolverTracer(io.StringIO(), collect=True)
    tracer.begin_run()
    tracer.fact_added(incompatibility)

    dep.marker = 'sys_platform == "win32"'

    fact = first_event(tracer, "fact")
    terms = fact["incompatibility"]["terms"]
    assert terms[0]["marker"] == ""


@pytest.mark.parametrize(
    ("incompatibility", "cause", "detail"),
    [
        (
            dependency_incompatibility("a", "b"),
            "dependency",
            {},
        ),
        (
            Incompatibility(
                [Term(Dependency("a", ">2.0"), True)],
                NoVersionsCauseError(),
            ),
            "no-versions",
            {},
        ),
        (
            Incompatibility(
                [Term(Dependency("a", "*"), True)],
                PythonCauseError(">=3.8", ">=3.7"),
            ),
            "python",
            {"python_version": ">=3.8", "root_python_version": ">=3.7"},
        ),
        (
            Incompatibility(
                [Term(Dependency("a", "*"), True)],
                PlatformCauseError("linux"),
            ),
            "platform",
            {"platform": "linux"},
        ),
        (
            Incompatibility(
                [Term(Dependency("root", "1.0"), False)],
                RootCauseError(),
            ),
            "root",
            {},
        ),
    ],
)
def test_incompatibility_nodes_for_each_cause_type(
    tracer: SolverTracer,
    incompatibility: Incompatibility,
    cause: str,
    detail: dict[str, str],
) -> None:
    tracer.begin_run()
    tracer.fact_added(incompatibility)

    node = first_event(tracer, "fact")["incompatibility"]
    assert node["id"] == "inc-1"
    assert node["cause"] == cause
    assert isinstance(node["terms"], list)
    assert node["terms"]
    for key, value in detail.items():
        assert node[key] == value


def test_conflict_cause_records_parent_ids() -> None:
    leaf = dependency_incompatibility("a", "b")
    other = dependency_incompatibility("c", "d")
    combined = Incompatibility(
        [
            Term(Dependency("e", "*"), True),
            Term(Dependency("f", "*"), True),
        ],
        ConflictCauseError(leaf, other),
    )

    tracer = SolverTracer(io.StringIO(), collect=True)
    tracer.begin_run()
    tracer.fact_added(combined)

    node = first_event(tracer, "fact")["incompatibility"]
    assert node["id"] == "inc-1"
    assert node["cause"] == "conflict"
    assert node["derived_from"] == {"conflict": "inc-2", "other": "inc-3"}


def test_failure_graph_and_external_incompatibilities(tracer: SolverTracer) -> None:
    leaf1 = dependency_incompatibility("a", "b")
    leaf2 = dependency_incompatibility("c", "d")
    combined = Incompatibility(
        [
            Term(Dependency("e", "*"), True),
            Term(Dependency("f", "*"), True),
        ],
        ConflictCauseError(leaf1, leaf2),
    )
    leaf3 = dependency_incompatibility("g", "h")
    root = Incompatibility(
        [
            Term(Dependency("i", "*"), True),
            Term(Dependency("j", "*"), True),
        ],
        ConflictCauseError(combined, leaf3),
    )

    tracer.begin_run()
    tracer.run_failed(root, attempted_solutions=2)

    event_records = records(tracer)
    failure = next(event for event in event_records if event["event"] == "failure")
    assert failure["event"] == "failure"
    graph = failure["graph"]
    # The terminal node is serialized first (inc-1), registering its parents
    # inc-2 (combined) and inc-3 (leaf3); graph DFS then numbers the leaves.
    assert set(graph) == {"inc-1", "inc-2", "inc-3", "inc-4", "inc-5"}
    assert failure["incompatibility"]["id"] == "inc-1"
    assert graph["inc-2"]["derived_from"] == {
        "conflict": "inc-4",
        "other": "inc-5",
    }
    assert graph["inc-1"]["derived_from"] == {
        "conflict": "inc-2",
        "other": "inc-3",
    }

    assert set(failure["external"]) == {"inc-3", "inc-4", "inc-5"}

    run_end = next(event for event in event_records if event["event"] == "run-end")
    assert run_end["outcome"] == "failure"
    assert run_end["attempted_solutions"] == 2


def test_events_are_valid_jsonl_with_continuous_sequence_and_common_fields(
    tracer: SolverTracer,
) -> None:
    root = ProjectPackage("myapp", "0.0.0")
    tracer.start_solve(root)
    tracer.begin_run()
    tracer.fact_added(dependency_incompatibility("a", "b"))
    tracer.run_succeeded([Package("a", "1.0.0")], attempted_solutions=1)
    tracer.finish_solve("success")

    event_records = records(tracer)
    assert [event["event"] for event in event_records] == [
        "solve-start",
        "run-start",
        "fact",
        "run-end",
        "solve-end",
    ]
    assert [event["seq"] for event in event_records] == [0, 1, 2, 3, 4]
    assert all(event["run"] == 0 for event in event_records[1:-1])
    assert event_records[0]["run"] is None
    assert event_records[-1]["run"] is None
    assert {"seq", "run", "event", "decision_level", "attempt"} <= set(event_records[0])
    assert event_records[0]["schema_version"] == TRACE_SCHEMA_VERSION
    assert event_records[0]["root"]["name"] == "myapp"
    assert event_records[-1] == {
        "seq": 4,
        "run": None,
        "event": "solve-end",
        "decision_level": 0,
        "attempt": 1,
        "outcome": "success",
        "runs": 1,
    }


def test_incompatibility_ids_are_reset_per_run(tracer: SolverTracer) -> None:
    tracer.begin_run()
    tracer.fact_added(dependency_incompatibility("a", "b"))
    tracer.run_marker_split(attempted_solutions=1)
    tracer.begin_run()
    tracer.fact_added(dependency_incompatibility("c", "d"))
    tracer.run_succeeded([], attempted_solutions=2)

    event_records = records(tracer)
    first_fact = next(event for event in event_records if event["event"] == "fact")
    second_fact = [event for event in event_records if event["event"] == "fact"][1]
    assert first_fact["run"] == 0
    assert first_fact["incompatibility"]["id"] == "inc-1"
    assert second_fact["run"] == 1
    assert second_fact["incompatibility"]["id"] == "inc-1"
    assert [event["seq"] for event in event_records] == list(range(len(event_records)))


def test_run_succeeded_decisions_are_sorted_by_name(tracer: SolverTracer) -> None:
    tracer.begin_run()
    tracer.run_succeeded(
        [Package("z", "1.0"), Package("a", "2.0"), Package("m", "3.0")],
        attempted_solutions=3,
    )

    run_end = first_event(tracer, "run-end")
    assert [package["name"] for package in run_end["decisions"]] == [
        "a",
        "m",
        "z",
    ]
    assert run_end["attempted_solutions"] == 3


class FailingSink(io.StringIO):
    def write(self, obj: str) -> int:
        raise OSError("disk full")


class UnexpectedErrorSink(io.StringIO):
    def write(self, obj: str) -> int:
        raise RuntimeError("unexpected sink failure")


def test_write_failure_disables_tracer_without_raising() -> None:
    tracer = SolverTracer(FailingSink())
    tracer.begin_run()
    tracer.fact_added(dependency_incompatibility("a", "b"))

    assert not tracer.is_recording()
    # Further calls stay silent and must not raise.
    tracer.fact_added(dependency_incompatibility("c", "d"))
    tracer.run_succeeded([], 1)
    tracer.close()


def test_unexpected_sink_error_disables_tracer_without_raising() -> None:
    tracer = SolverTracer(UnexpectedErrorSink())
    tracer.begin_run()
    tracer.fact_added(dependency_incompatibility("a", "b"))

    assert not tracer.is_recording()
    tracer.run_succeeded([], 1)
    tracer.close()


def test_null_tracer_is_a_silent_noop() -> None:
    assert NULL_TRACER.is_recording() is False
    NULL_TRACER.start_solve(ProjectPackage("root", "1.0"))
    NULL_TRACER.begin_run()
    NULL_TRACER.fact_added(dependency_incompatibility("a", "b"))
    NULL_TRACER.derived(
        Dependency("a", "1.0"),
        False,
        dependency_incompatibility("c", "d"),
        0,
    )
    NULL_TRACER.conflict_detected(dependency_incompatibility("e", "f"))
    NULL_TRACER.backtracked(3, 1, ["a"])
    NULL_TRACER.no_versions(Dependency("a", ">9"))
    NULL_TRACER.run_succeeded([], 1)
    NULL_TRACER.run_marker_split(1)
    NULL_TRACER.run_error(RuntimeError("boom"), 1)
    NULL_TRACER.finish_solve("success")
    NULL_TRACER.close()


def test_run_error_is_recorded(tracer: SolverTracer) -> None:
    tracer.begin_run()
    tracer.run_error(RuntimeError("boom"), attempted_solutions=1)

    run_end = first_event(tracer, "run-end")
    assert run_end["outcome"] == "error"
    assert run_end["error_type"] == "RuntimeError"
    assert run_end["error"] == "boom"


def test_run_context_serializes_overrides_sorted(
    tracer: SolverTracer,
) -> None:
    package_b = Package("b", "1.0")
    package_a = Package("a", "1.0")
    overrides = {
        package_b: {"z": Dependency("z", "^1"), "a": Dependency("a", "^2")},
        package_a: {"m": Dependency("m", "^3")},
    }

    tracer.set_run_context(overrides)
    tracer.begin_run()

    run_start = records(tracer)[0]
    packages = [entry["package"]["name"] for entry in run_start["overrides"]]
    assert packages == ["a", "b"]
    first = run_start["overrides"][0]
    assert [dep["name"] for dep in first["dependencies"]] == ["m"]
    second = run_start["overrides"][1]
    assert [dep["name"] for dep in second["dependencies"]] == ["a", "z"]


def test_run_context_none_omits_field(tracer: SolverTracer) -> None:
    tracer.set_run_context(None)
    tracer.begin_run()

    assert "overrides" not in records(tracer)[0]
