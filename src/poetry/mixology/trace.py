from __future__ import annotations

import io
import json

from pathlib import Path
from typing import TYPE_CHECKING
from typing import Protocol
from typing import TextIO

from poetry.mixology.incompatibility_cause import ConflictCauseError
from poetry.mixology.incompatibility_cause import DependencyCauseError
from poetry.mixology.incompatibility_cause import NoVersionsCauseError
from poetry.mixology.incompatibility_cause import PlatformCauseError
from poetry.mixology.incompatibility_cause import PythonCauseError
from poetry.mixology.incompatibility_cause import RootCauseError


if TYPE_CHECKING:
    from collections.abc import Mapping
    from collections.abc import Sequence

    from poetry.core.packages.dependency import Dependency
    from poetry.core.packages.package import Package
    from poetry.core.packages.project_package import ProjectPackage
    from poetry.core.version.markers import BaseMarker

    from poetry.mixology.assignment import Assignment
    from poetry.mixology.incompatibility import Incompatibility
    from poetry.mixology.partial_solution import PartialSolution
    from poetry.mixology.term import Term


TRACE_SCHEMA_VERSION = "1"

_UNSET: object = object()


def _cause_name(cause: object) -> str:
    if isinstance(cause, DependencyCauseError):
        return "dependency"
    if isinstance(cause, NoVersionsCauseError):
        return "no-versions"
    if isinstance(cause, PythonCauseError):
        return "python"
    if isinstance(cause, PlatformCauseError):
        return "platform"
    if isinstance(cause, RootCauseError):
        return "root"
    if isinstance(cause, ConflictCauseError):
        return "conflict"
    return "unknown"


def _source_fields(obj: object) -> dict[str, object]:
    fields: dict[str, object] = {}
    for attr in (
        "source_type",
        "source_url",
        "source_reference",
        "source_name",
        "source_subdirectory",
    ):
        value = getattr(obj, attr, None)
        if value:
            fields[attr] = value
    return fields


def serialize_dependency(dependency: Dependency) -> dict[str, object]:
    """
    Converts a dependency to a deterministic, JSON-safe snapshot.

    Only read accesses are performed and all values are converted to
    primitives immediately, so later mutations of the dependency
    (e.g. ``transitive_marker``) cannot affect the recorded snapshot.
    """
    data: dict[str, object] = {
        "name": dependency.name,
        "complete_name": dependency.complete_name,
        "constraint": dependency.pretty_constraint,
        "marker": str(dependency.marker),
        "extras": sorted(dependency.extras),
        "groups": sorted(dependency.groups),
        "direct_origin": dependency.is_direct_origin(),
    }
    data.update(_source_fields(dependency))
    return data


def serialize_package(package: Package) -> dict[str, object]:
    data: dict[str, object] = {
        "name": package.name,
        "complete_name": package.complete_name,
        "version": str(package.version),
        "full_pretty_version": package.full_pretty_version,
        "features": sorted(package.features),
        "yanked": package.yanked,
    }
    data.update(_source_fields(package))
    return data


def serialize_term(term: Term) -> dict[str, object]:
    data = serialize_dependency(term.dependency)
    data["positive"] = term.is_positive()
    return data


class Tracer(Protocol):
    """
    Observation sink for the internal events of a version solver run.

    All arguments are passed as references to existing solver objects;
    implementations decide whether (and how) to serialize them.
    """

    def is_recording(self) -> bool: ...

    def bind_solution(self, solution: PartialSolution) -> None: ...

    def set_run_context(
        self, overrides: Mapping[Package, Mapping[str, Dependency]] | None
    ) -> None: ...

    def start_solve(self, root: ProjectPackage) -> None: ...

    def begin_run(self) -> None: ...

    def finish_solve(self, outcome: str) -> None: ...

    def close(self) -> None: ...

    def fact_added(self, incompatibility: Incompatibility) -> None: ...

    def decision_made(
        self,
        package: Package,
        dependency: Dependency,
        preference: str,
        locked: bool,
        use_latest: bool,
        candidate_count: int,
    ) -> None: ...

    def candidate_rejected(
        self,
        package: Package,
        dependency: Dependency,
        preference: str,
        locked: bool,
        use_latest: bool,
        candidate_count: int,
        conflicts: Sequence[Incompatibility],
    ) -> None: ...

    def derived(
        self,
        dependency: Dependency,
        is_positive: bool,
        cause: Incompatibility,
        assignment_index: int,
    ) -> None: ...

    def conflict_detected(self, incompatibility: Incompatibility) -> None: ...

    def conflict_resolution(
        self,
        most_recent_term: Term,
        most_recent_satisfier: Assignment,
        difference_present: bool,
        resulting: Incompatibility,
    ) -> None: ...

    def backtracked(
        self, from_level: int, to_level: int, undone_decisions: Sequence[str]
    ) -> None: ...

    def no_versions(self, dependency: Dependency) -> None: ...

    def marker_split(
        self,
        dependency_a: Dependency,
        dependency_b: Dependency,
        world_a: BaseMarker,
        world_b: BaseMarker,
    ) -> None: ...

    def run_succeeded(
        self, packages: Sequence[Package], attempted_solutions: int
    ) -> None: ...

    def run_failed(
        self, incompatibility: Incompatibility, attempted_solutions: int
    ) -> None: ...

    def run_marker_split(self, attempted_solutions: int) -> None: ...

    def run_error(self, error: BaseException, attempted_solutions: int) -> None: ...


class NullTracer:
    """
    Tracer used when decision tracing is disabled.

    Every method is a no-op: no serialization, no IO and no control flow
    are performed, so solver behavior and performance are unchanged.
    """

    __slots__ = ()

    def is_recording(self) -> bool:
        return False

    def bind_solution(self, solution: PartialSolution) -> None:
        return None

    def set_run_context(
        self, overrides: Mapping[Package, Mapping[str, Dependency]] | None
    ) -> None:
        return None

    def start_solve(self, root: ProjectPackage) -> None:
        return None

    def begin_run(self) -> None:
        return None

    def finish_solve(self, outcome: str) -> None:
        return None

    def close(self) -> None:
        return None

    def fact_added(self, incompatibility: Incompatibility) -> None:
        return None

    def decision_made(
        self,
        package: Package,
        dependency: Dependency,
        preference: str,
        locked: bool,
        use_latest: bool,
        candidate_count: int,
    ) -> None:
        return None

    def candidate_rejected(
        self,
        package: Package,
        dependency: Dependency,
        preference: str,
        locked: bool,
        use_latest: bool,
        candidate_count: int,
        conflicts: Sequence[Incompatibility],
    ) -> None:
        return None

    def derived(
        self,
        dependency: Dependency,
        is_positive: bool,
        cause: Incompatibility,
        assignment_index: int,
    ) -> None:
        return None

    def conflict_detected(self, incompatibility: Incompatibility) -> None:
        return None

    def conflict_resolution(
        self,
        most_recent_term: Term,
        most_recent_satisfier: Assignment,
        difference_present: bool,
        resulting: Incompatibility,
    ) -> None:
        return None

    def backtracked(
        self, from_level: int, to_level: int, undone_decisions: Sequence[str]
    ) -> None:
        return None

    def no_versions(self, dependency: Dependency) -> None:
        return None

    def marker_split(
        self,
        dependency_a: Dependency,
        dependency_b: Dependency,
        world_a: BaseMarker,
        world_b: BaseMarker,
    ) -> None:
        return None

    def run_succeeded(
        self, packages: Sequence[Package], attempted_solutions: int
    ) -> None:
        return None

    def run_failed(
        self, incompatibility: Incompatibility, attempted_solutions: int
    ) -> None:
        return None

    def run_marker_split(self, attempted_solutions: int) -> None:
        return None

    def run_error(self, error: BaseException, attempted_solutions: int) -> None:
        return None


NULL_TRACER = NullTracer()


class SolverTracer:
    """
    Records solver events as JSON Lines.

    Each event is written as a single, self-contained JSON object followed by
    a newline and flushed immediately. Records contain only deterministic
    fields: two runs with identical inputs produce byte-identical files.
    """

    def __init__(self, sink: TextIO, *, collect: bool = False) -> None:
        self._sink = sink
        self._collect = collect
        self.events: list[dict[str, object]] = []

        self._disabled = False
        self._seq = -1
        self._run = -1
        self._solution: PartialSolution | None = None
        self._incompatibility_ids: dict[int, str] = {}
        self._incompatibility_counter = 0
        self._pending_run_context: list[dict[str, object]] | None = None

    def disable(self) -> None:
        self._disabled = True

    def is_recording(self) -> bool:
        return not self._disabled

    def bind_solution(self, solution: PartialSolution) -> None:
        self._solution = solution

    def set_run_context(
        self, overrides: Mapping[Package, Mapping[str, Dependency]] | None
    ) -> None:
        if self._disabled or overrides is None:
            self._pending_run_context = None
            return
        self._pending_run_context = [
            {
                "package": serialize_package(package),
                "dependencies": [
                    serialize_dependency(dependencies[name])
                    for name in sorted(dependencies)
                ],
            }
            for package, dependencies in sorted(
                overrides.items(), key=lambda item: item[0].name
            )
        ]

    def start_solve(self, root: ProjectPackage) -> None:
        self._emit(
            "solve-start",
            {
                "schema_version": TRACE_SCHEMA_VERSION,
                "root": {
                    "name": root.name,
                    "complete_name": root.complete_name,
                    "version": str(root.version),
                },
            },
            level=0,
            attempt=1,
            run=None,
        )

    def begin_run(self) -> None:
        self._run += 1
        self._incompatibility_ids = {}
        self._incompatibility_counter = 0
        payload: dict[str, object] = {}
        if self._pending_run_context:
            payload["overrides"] = self._pending_run_context
        self._pending_run_context = None
        self._emit("run-start", payload, level=0, attempt=1)

    def finish_solve(self, outcome: str) -> None:
        self._emit("solve-end", {"outcome": outcome, "runs": self._run + 1}, run=None)

    def close(self) -> None:
        if self._disabled:
            return
        try:
            self._sink.flush()
            self._sink.close()
        except Exception:  # noqa: BLE001
            self.disable()

    def fact_added(self, incompatibility: Incompatibility) -> None:
        self._emit(
            "fact", {"incompatibility": self._incompatibility_node(incompatibility)}
        )

    def decision_made(
        self,
        package: Package,
        dependency: Dependency,
        preference: str,
        locked: bool,
        use_latest: bool,
        candidate_count: int,
    ) -> None:
        self._emit(
            "decision",
            {
                "package": serialize_package(package),
                "dependency": serialize_dependency(dependency),
                "preference": preference,
                "locked": locked,
                "use_latest": use_latest,
                "candidate_count": candidate_count,
            },
        )

    def candidate_rejected(
        self,
        package: Package,
        dependency: Dependency,
        preference: str,
        locked: bool,
        use_latest: bool,
        candidate_count: int,
        conflicts: Sequence[Incompatibility],
    ) -> None:
        self._emit(
            "candidate-rejected",
            {
                "package": serialize_package(package),
                "dependency": serialize_dependency(dependency),
                "preference": preference,
                "locked": locked,
                "use_latest": use_latest,
                "candidate_count": candidate_count,
                "conflicts": [
                    self._incompatibility_node(incompatibility)
                    for incompatibility in conflicts
                ],
            },
        )

    def derived(
        self,
        dependency: Dependency,
        is_positive: bool,
        cause: Incompatibility,
        assignment_index: int,
    ) -> None:
        self._emit(
            "derived",
            {
                "dependency": serialize_dependency(dependency),
                "positive": is_positive,
                "cause_id": self._incompatibility_id(cause),
                "index": assignment_index,
            },
        )

    def conflict_detected(self, incompatibility: Incompatibility) -> None:
        self._emit(
            "conflict",
            {"incompatibility": self._incompatibility_node(incompatibility)},
        )

    def conflict_resolution(
        self,
        most_recent_term: Term,
        most_recent_satisfier: Assignment,
        difference_present: bool,
        resulting: Incompatibility,
    ) -> None:
        self._emit(
            "conflict-resolution",
            {
                "most_recent_term": serialize_term(most_recent_term),
                "satisfier": self._serialize_assignment(most_recent_satisfier),
                "difference": difference_present,
                "incompatibility": self._incompatibility_node(resulting),
            },
        )

    def backtracked(
        self, from_level: int, to_level: int, undone_decisions: Sequence[str]
    ) -> None:
        self._emit(
            "backtrack",
            {
                "from_level": from_level,
                "to_level": to_level,
                "undone_decisions": list(undone_decisions),
            },
            level=to_level,
        )

    def no_versions(self, dependency: Dependency) -> None:
        self._emit("no-versions", {"dependency": serialize_dependency(dependency)})

    def marker_split(
        self,
        dependency_a: Dependency,
        dependency_b: Dependency,
        world_a: BaseMarker,
        world_b: BaseMarker,
    ) -> None:
        self._emit(
            "marker-split",
            {
                "package": dependency_a.name,
                "requirements": [
                    serialize_dependency(dependency_a),
                    serialize_dependency(dependency_b),
                ],
                "worlds": [str(world_a), str(world_b)],
            },
        )

    def run_succeeded(
        self, packages: Sequence[Package], attempted_solutions: int
    ) -> None:
        decisions = [serialize_package(package) for package in packages]
        decisions.sort(key=lambda package: str(package["complete_name"]))
        self._emit(
            "run-end",
            {
                "outcome": "success",
                "attempted_solutions": attempted_solutions,
                "decisions": decisions,
            },
        )

    def run_failed(
        self, incompatibility: Incompatibility, attempted_solutions: int
    ) -> None:
        graph: dict[str, dict[str, object]] = {}
        stack = [incompatibility]
        while stack:
            current = stack.pop()
            incompatibility_id = self._incompatibility_id(current)
            if incompatibility_id in graph:
                continue
            graph[incompatibility_id] = self._incompatibility_node(current)
            cause = current.cause
            if isinstance(cause, ConflictCauseError):
                stack.append(cause.conflict)
                stack.append(cause.other)

        external = list(
            dict.fromkeys(
                self._incompatibility_id(leaf)
                for leaf in incompatibility.external_incompatibilities
            )
        )

        self._emit(
            "failure",
            {
                "incompatibility": self._incompatibility_node(incompatibility),
                "graph": graph,
                "external": external,
            },
        )
        self._emit(
            "run-end",
            {"outcome": "failure", "attempted_solutions": attempted_solutions},
        )

    def run_marker_split(self, attempted_solutions: int) -> None:
        self._emit(
            "run-end",
            {"outcome": "marker-split", "attempted_solutions": attempted_solutions},
        )

    def run_error(self, error: BaseException, attempted_solutions: int) -> None:
        self._emit(
            "run-end",
            {
                "outcome": "error",
                "attempted_solutions": attempted_solutions,
                "error_type": type(error).__name__,
                "error": str(error),
            },
        )

    def _incompatibility_id(self, incompatibility: Incompatibility) -> str:
        key = id(incompatibility)
        incompatibility_id = self._incompatibility_ids.get(key)
        if incompatibility_id is None:
            self._incompatibility_counter += 1
            incompatibility_id = f"inc-{self._incompatibility_counter}"
            self._incompatibility_ids[key] = incompatibility_id
        return incompatibility_id

    def _incompatibility_node(
        self, incompatibility: Incompatibility
    ) -> dict[str, object]:
        cause = incompatibility.cause
        data: dict[str, object] = {
            "id": self._incompatibility_id(incompatibility),
            "cause": _cause_name(cause),
            "terms": [serialize_term(term) for term in incompatibility.terms],
        }
        if isinstance(cause, PythonCauseError):
            data["python_version"] = cause.python_version
            data["root_python_version"] = cause.root_python_version
        elif isinstance(cause, PlatformCauseError):
            data["platform"] = cause.platform
        elif isinstance(cause, ConflictCauseError):
            data["derived_from"] = {
                "conflict": self._incompatibility_id(cause.conflict),
                "other": self._incompatibility_id(cause.other),
            }
        return data

    def _serialize_assignment(self, assignment: Assignment) -> dict[str, object]:
        data: dict[str, object] = {
            "kind": "decision" if assignment.is_decision() else "derivation",
            "decision_level": assignment.decision_level,
            "index": assignment.index,
            "term": serialize_term(assignment),
        }
        cause = assignment.cause
        if cause is not None:
            data["cause_id"] = self._incompatibility_id(cause)
        return data

    def _emit(
        self,
        event: str,
        payload: dict[str, object],
        level: int | None = None,
        attempt: int | None = None,
        run: int | object | None = _UNSET,
    ) -> None:
        if self._disabled:
            return
        self._seq += 1
        decision_level = (
            level
            if level is not None
            else (self._solution.decision_level if self._solution else 0)
        )
        attempted_solutions = (
            attempt
            if attempt is not None
            else (self._solution.attempted_solutions if self._solution else 1)
        )
        record = {
            "seq": self._seq,
            "run": self._run if run is _UNSET else run,
            "event": event,
            "decision_level": decision_level,
            "attempt": attempted_solutions,
            **payload,
        }
        try:
            self._sink.write(json.dumps(record, ensure_ascii=False) + "\n")
            self._sink.flush()
        except Exception:  # noqa: BLE001
            # Tracing must never break resolution: any serialization or
            # sink failure disables the trace silently.
            self.disable()
            return
        if self._collect:
            self.events.append(record)


class FileSolverTracer(SolverTracer):
    """
    A ``SolverTracer`` that owns its JSONL output file.

    The file is opened in truncate mode (each top-level solve overwrites the
    previous trace) with LF line endings so traces are comparable across
    platforms. If the file cannot be opened, tracing degrades to a disabled,
    in-memory tracer and the error is exposed via ``io_error``.
    """

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self.io_error: OSError | None = None
        sink: TextIO
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            sink = self._path.open("w", encoding="utf-8", newline="\n")
        except OSError as exc:
            self.io_error = exc
            sink = io.StringIO()
        super().__init__(sink)
        if self.io_error is not None:
            self.disable()

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        if self.io_error is not None:
            return
        super().close()
