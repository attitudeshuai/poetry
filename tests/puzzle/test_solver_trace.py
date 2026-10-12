from __future__ import annotations

import json

from typing import TYPE_CHECKING
from typing import Any

from cleo.io.buffered_io import BufferedIO
from cleo.io.null_io import NullIO

from poetry.config.config import Config
from poetry.factory import Factory
from poetry.puzzle.solver import Solver
from tests.helpers import get_package


if TYPE_CHECKING:
    from pathlib import Path

    import pytest

    from poetry.core.packages.project_package import ProjectPackage

    from poetry.repositories import Repository
    from poetry.repositories import RepositoryPool


def trace_config(
    tmp_path: Path, *, enabled: bool = True, path: str | None = None
) -> Config:
    config = Config(use_environment=False)
    if enabled:
        config.merge(
            {
                "solver": {
                    "trace": True,
                    "trace-path": str(path) if path is not None else None,
                }
            }
        )
    return config


def read_trace(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def make_solver(
    package: ProjectPackage, pool: RepositoryPool, config: Config, io: Any = None
) -> Solver:
    return Solver(package, pool, [], [], io or NullIO(), config=config)


def test_trace_file_written_when_enabled(
    package: ProjectPackage, repo: Repository, pool: RepositoryPool, tmp_path: Path
) -> None:
    package.add_dependency(Factory.create_dependency("a", "*"))
    repo.add_package(get_package("a", "1.0.0"))
    trace_path = tmp_path / "solver-trace.jsonl"

    make_solver(package, pool, trace_config(tmp_path, path=str(trace_path))).solve()

    events = read_trace(trace_path)
    assert events[0]["event"] == "solve-start"
    assert events[0]["root"]["name"] == "root"
    assert events[-1]["event"] == "solve-end"
    assert events[-1]["outcome"] == "success"
    assert events[-1]["runs"] == 1
    assert [event["seq"] for event in events] == list(range(len(events)))
    assert {event["run"] for event in events[1:-1]} == {0}


def test_no_trace_file_when_disabled(
    package: ProjectPackage,
    repo: Repository,
    pool: RepositoryPool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    package.add_dependency(Factory.create_dependency("a", "*"))
    repo.add_package(get_package("a", "1.0.0"))

    make_solver(package, pool, trace_config(tmp_path, enabled=False)).solve()

    assert not (tmp_path / "poetry-solver-trace.jsonl").exists()


def test_trace_is_written_to_default_path_under_cwd(
    package: ProjectPackage,
    repo: Repository,
    pool: RepositoryPool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    package.add_dependency(Factory.create_dependency("a", "*"))
    repo.add_package(get_package("a", "1.0.0"))

    make_solver(package, pool, trace_config(tmp_path)).solve()

    assert (tmp_path / "poetry-solver-trace.jsonl").exists()


def test_trace_file_is_overwritten_not_appended(
    package: ProjectPackage, repo: Repository, pool: RepositoryPool, tmp_path: Path
) -> None:
    package.add_dependency(Factory.create_dependency("a", "*"))
    repo.add_package(get_package("a", "1.0.0"))
    trace_path = tmp_path / "solver-trace.jsonl"
    config = trace_config(tmp_path, path=str(trace_path))

    make_solver(package, pool, config).solve()
    first_contents = trace_path.read_text(encoding="utf-8")
    make_solver(package, pool, config).solve()
    second_contents = trace_path.read_text(encoding="utf-8")

    assert first_contents == second_contents
    events = read_trace(trace_path)
    assert events[0]["seq"] == 0
    assert [event["event"] for event in events].count("solve-start") == 1


def test_marker_split_runs_are_segments_of_one_trace_file(
    package: ProjectPackage, repo: Repository, pool: RepositoryPool, tmp_path: Path
) -> None:
    package.add_dependency(
        Factory.create_dependency(
            "A", {"version": "1.0", "markers": "sys_platform != 'darwin'"}
        )
    )
    package.add_dependency(
        Factory.create_dependency(
            "B", {"version": "2.0", "markers": "sys_platform == 'darwin'"}
        )
    )
    package_a = get_package("A", "1.0")
    package_a.add_dependency(Factory.create_dependency("B", "1.0"))
    repo.add_package(package_a)
    repo.add_package(get_package("B", "1.0"))
    repo.add_package(get_package("B", "2.0"))

    trace_path = tmp_path / "solver-trace.jsonl"
    transaction = make_solver(
        package, pool, trace_config(tmp_path, path=str(trace_path))
    ).solve()

    events = read_trace(trace_path)
    run_starts = [event for event in events if event["event"] == "run-start"]
    run_ends = [event for event in events if event["event"] == "run-end"]
    assert [event["run"] for event in run_starts] == list(range(len(run_starts)))
    assert len(run_starts) >= 2
    assert any(event["outcome"] == "marker-split" for event in run_ends)
    assert all(event["outcome"] == "success" for event in run_ends[-1:])
    later_run_starts = [event for event in run_starts if event["run"] > 0]
    assert all("overrides" in event for event in later_run_starts)
    assert any(event["event"] == "marker-split" for event in events)
    assert events[-1]["event"] == "solve-end"
    assert events[-1]["outcome"] == "success"
    assert events[-1]["runs"] == len(run_starts)

    solved = {op.package.name for op in transaction.calculate_operations()}
    assert {"a", "b"} <= solved


def test_unwritable_trace_path_warns_once_and_still_solves(
    package: ProjectPackage, repo: Repository, pool: RepositoryPool, tmp_path: Path
) -> None:
    package.add_dependency(Factory.create_dependency("a", "*"))
    repo.add_package(get_package("a", "1.0.0"))
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("x", encoding="utf-8")
    bad_path = blocker / "trace.jsonl"
    io = BufferedIO()

    transaction = make_solver(
        package,
        pool,
        trace_config(tmp_path, path=str(bad_path)),
        io=io,
    ).solve()

    assert {op.package.name for op in transaction.calculate_operations()} == {"a"}
    warning = io.fetch_error()
    assert "unable to write the solver decision trace" in warning
    assert warning.count("unable to write the solver decision trace") == 1
