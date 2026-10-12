from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import os
import re
import threading
import uuid

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING
from typing import Any

from packaging.utils import canonicalize_name

from poetry.installation.operations import Update


if TYPE_CHECKING:
    from pathlib import Path

    from poetry.installation.operations.operation import Operation
    from poetry.utils.env import Env


logger = logging.getLogger(__name__)

FORMAT_VERSION = 1

# Run statuses
RUN_RUNNING = "running"
RUN_COMPLETED = "completed"
RUN_FAILED = "failed"
RUN_INTERRUPTED = "interrupted"

INCOMPLETE_RUN_STATUSES = {RUN_RUNNING, RUN_FAILED, RUN_INTERRUPTED}

# Operation statuses
OP_PENDING = "pending"
OP_RUNNING = "running"
OP_COMPLETED = "completed"
OP_SKIPPED = "skipped"
OP_FAILED = "failed"
OP_INTERRUPTED = "interrupted"

# Operation phases (granular enough to tell "half installed" and
# "old version removed, new version not installed" apart)
PHASE_PREPARE = "prepare"
PHASE_REMOVE_OLD = "remove-old"
PHASE_OLD_REMOVED = "old-removed"
PHASE_INSTALL = "install"
PHASE_UNINSTALL = "uninstall"

# Reconciliation fact categories
FACT_VERIFIED = "verified"
FACT_SKIPPED = "skipped"
FACT_COMPLETED_NOT_PRESENT = "completed-not-present"
FACT_REMOVED_NOT_REINSTALLED = "removed-not-reinstalled"
FACT_INCOMPLETE = "incomplete"
FACT_NOT_STARTED = "not-started"
FACT_UNCHECKED = "unchecked"


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def operation_key(operation: Operation) -> str:
    """
    A stable identity of an operation that survives being planned again in a
    later run (e.g. after an interruption).
    """
    name = canonicalize_name(operation.package.name)
    version = str(operation.package.version)
    if isinstance(operation, Update):
        return (
            f"update:{name}:{operation.initial_package.version}->{version}"
        )
    return f"{operation.job_type}:{name}:{version}"


def describe_operation(operation: Operation, seq: int) -> dict[str, Any]:
    package = operation.package
    initial_version: str | None = None
    if isinstance(operation, Update):
        initial_version = str(operation.initial_package.version)

    priority = operation.priority
    if math.isinf(priority):
        priority = None

    return {
        "seq": seq,
        "job_type": operation.job_type,
        "name": package.name,
        "pretty_name": package.pretty_name,
        "version": str(package.version),
        "pretty_version": package.full_pretty_version,
        "initial_version": initial_version,
        "source_type": package.source_type,
        "priority": priority,
        "skipped": operation.skipped,
        "skip_reason": operation.skip_reason,
    }


def verify_operation(operation: Operation, env: Env) -> bool | None:
    """
    Check the recorded outcome of an operation against the actual
    environment.

    Returns ``True`` if the environment matches the desired operation result,
    ``False`` if it does not, and ``None`` if the state cannot be determined
    (in which case the operation must be executed again to be safe).

    This is a read-only check: nothing is modified or rolled back.
    """
    try:
        if operation.job_type == "uninstall":
            dist = env.site_packages.find_distribution(operation.package.name)
            return dist is None

        dist = env.site_packages.find_distribution(operation.package.name)
        if dist is None:
            return False

        expected = str(operation.package.version)
        try:
            from poetry.core.constraints.version import parse_version

            return parse_version(str(dist.version)) == parse_version(expected)
        except Exception:  # noqa: BLE001
            return str(dist.version) == expected
    except Exception:
        logger.debug(
            "Could not verify operation %s against the environment",
            operation,
            exc_info=True,
        )
        return None


@dataclass
class ReconciliationEntry:
    seq: int
    plan: dict[str, Any]
    state: dict[str, Any] | None
    category: str
    detail: str | None = None

    @property
    def action(self) -> str:
        plan = self.plan
        name = plan["pretty_name"]
        version = plan["pretty_version"]
        if plan["job_type"] == "install":
            return f"Installing {name} ({version})"
        if plan["job_type"] == "uninstall":
            return f"Removing {name} ({version})"
        return (
            f"Updating {name} ({plan['initial_version']} -> {version})"
        )


class ReconciliationReport:
    """
    Facts derived by comparing a recorded installation plan and its recorded
    progress with the current state of the environment. The report never
    suggests or performs any rollback; it only states what happened.
    """

    def __init__(
        self,
        run: InstallJournal,
        entries: list[ReconciliationEntry],
        env_checked: bool,
    ) -> None:
        self.run = run
        self.entries = entries
        self.env_checked = env_checked

    def by_category(self, category: str) -> list[ReconciliationEntry]:
        return [e for e in self.entries if e.category == category]

    @property
    def pending_entries(self) -> list[ReconciliationEntry]:
        return [
            e
            for e in self.entries
            if e.category
            not in {FACT_VERIFIED, FACT_SKIPPED}
        ]

    def render(self) -> str:
        sections: list[str] = []

        verified = self.by_category(FACT_VERIFIED)
        skipped = self.by_category(FACT_SKIPPED)
        missing = self.by_category(FACT_COMPLETED_NOT_PRESENT)
        removed = self.by_category(FACT_REMOVED_NOT_REINSTALLED)
        incomplete = self.by_category(FACT_INCOMPLETE)
        not_started = self.by_category(FACT_NOT_STARTED)
        unchecked = self.by_category(FACT_UNCHECKED)

        if verified:
            sections.append(
                "Completed and present in the environment:\n"
                + "\n".join(f"  - {e.action}" for e in verified)
            )
        if skipped:
            sections.append(
                "Skipped:\n"
                + "\n".join(
                    f"  - {e.action}: {e.detail or 'skipped'}" for e in skipped
                )
            )
        if removed:
            sections.append(
                "Old version removed, new version not installed:\n"
                + "\n".join(f"  - {e.action}: {e.detail}" for e in removed)
            )
        if incomplete:
            sections.append(
                "Did not complete:\n"
                + "\n".join(
                    f"  - {e.action}: {e.detail or 'interrupted'}"
                    for e in incomplete
                )
            )
        if not_started:
            sections.append(
                "Not started:\n"
                + "\n".join(f"  - {e.action}" for e in not_started)
            )
        if missing:
            sections.append(
                "Recorded as completed but not found in the environment:\n"
                + "\n".join(f"  - {e.action}" for e in missing)
            )
        if unchecked:
            sections.append(
                "Recorded as completed, environment state could not be"
                " checked:\n"
                + "\n".join(f"  - {e.action}" for e in unchecked)
            )

        return "\n\n".join(sections)

    def render_pending(self) -> str:
        """
        Compact rendering of the gap between the plan and the environment
        only; one line per fact, no headings.
        """
        lines: list[str] = []
        for entry in self.pending_entries:
            if entry.category == FACT_REMOVED_NOT_REINSTALLED:
                lines.append(f"  - {entry.action}: {entry.detail}")
            elif entry.category == FACT_INCOMPLETE:
                lines.append(
                    f"  - {entry.action}: {entry.detail or 'interrupted'}"
                )
            elif entry.category == FACT_NOT_STARTED:
                lines.append(f"  - {entry.action}: not started")
            elif entry.category == FACT_COMPLETED_NOT_PRESENT:
                lines.append(
                    f"  - {entry.action}: recorded as completed, but not"
                    " found in the environment"
                )
            elif entry.category == FACT_UNCHECKED:
                lines.append(
                    f"  - {entry.action}: recorded as completed, but it could"
                    " not be verified in the environment"
                )
        return "\n".join(lines)


class InstallJournal:
    """
    The persistent record of one installation run: the full plan created when
    the batch starts, plus the current state of every operation.

    Each process writes its own run file, so concurrent installations never
    overwrite each other's records. Updates are atomic (temp file + replace)
    and serialized within the process.
    """

    def __init__(self, path: Path, data: dict[str, Any]) -> None:
        self._path = path
        self._data = data
        self._lock = threading.RLock()

    @classmethod
    def create(
        cls,
        directory: Path,
        *,
        env: Env,
        project_path: Path | None,
        operations: list[Operation],
        resumed_from: str | None = None,
    ) -> InstallJournal:
        directory.mkdir(parents=True, exist_ok=True)

        started = datetime.now().astimezone()
        run_id = (
            f"{started.strftime('%Y%m%dT%H%M%S.%f')}-{os.getpid()}-"
            f"{uuid.uuid4().hex[:8]}"
        )
        path = directory / f"{run_id}.json"

        python_version: str | None = None
        with contextlib.suppress(Exception):
            python_version = env.marker_env["python_full_version"]

        project: dict[str, Any] | None = None
        if project_path is not None:
            project = {
                "path": str(project_path),
            }

        plan = [
            describe_operation(operation, seq)
            for seq, operation in enumerate(operations)
        ]
        data: dict[str, Any] = {
            "format_version": FORMAT_VERSION,
            "run_id": run_id,
            "pid": os.getpid(),
            "started_at": started.isoformat(timespec="seconds"),
            "finished_at": None,
            "status": RUN_RUNNING,
            "resumed_from": resumed_from,
            "environment": {
                "path": str(env.path),
                "python_version": python_version,
            },
            "project": project,
            "plan": plan,
            "operations": {
                str(item["seq"]): {
                    "status": (
                        OP_SKIPPED if item["skipped"] else OP_PENDING
                    ),
                    "phase": None,
                    "attempts": 0,
                    "started_at": None,
                    "finished_at": None,
                    "error": None,
                    "skip_reason": item["skip_reason"],
                }
                for item in plan
            },
        }

        journal = cls(path, data)
        journal._persist()
        return journal

    @classmethod
    def load(cls, path: Path) -> InstallJournal:
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls(path, data)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def run_id(self) -> str:
        return str(self._data["run_id"])

    @property
    def status(self) -> str:
        return str(self._data["status"])

    @property
    def started_at(self) -> str:
        return str(self._data.get("started_at", ""))

    @property
    def finished_at(self) -> str | None:
        finished = self._data.get("finished_at")
        return str(finished) if finished is not None else None

    @property
    def plan(self) -> list[dict[str, Any]]:
        return list(self._data["plan"])

    @property
    def is_incomplete(self) -> bool:
        return self.status in INCOMPLETE_RUN_STATUSES

    def _persist(self) -> None:
        with self._lock:
            tmp_path = self._path.with_name(
                f".{self._path.name}.tmp.{uuid.uuid4().hex[:8]}"
            )
            tmp_path.write_text(
                json.dumps(self._data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            os.replace(tmp_path, self._path)

    def state(self, seq: int) -> dict[str, Any] | None:
        return self._data["operations"].get(str(seq))

    def update_operation(
        self,
        seq: int,
        *,
        status: str | None = None,
        phase: str | None = None,
        error: BaseException | None = None,
    ) -> None:
        with self._lock:
            state = self._data["operations"].get(str(seq))
            if state is None:
                return

            if status is not None:
                if status == OP_RUNNING:
                    state["attempts"] += 1
                    state["started_at"] = _now()
                elif status in {
                    OP_COMPLETED,
                    OP_SKIPPED,
                    OP_FAILED,
                    OP_INTERRUPTED,
                }:
                    state["finished_at"] = _now()
                state["status"] = status

            if phase is not None:
                state["phase"] = phase

            if error is not None:
                state["error"] = {
                    "type": type(error).__name__,
                    "message": str(error),
                }

            self._persist()

    def finish(self, status: str) -> None:
        with self._lock:
            # An interrupted/killed process leaves operations in "running"
            # state; record that fact instead of pretending they are pending.
            for state in self._data["operations"].values():
                if state["status"] == OP_RUNNING:
                    state["status"] = OP_INTERRUPTED
                    state["finished_at"] = _now()
            self._data["status"] = status
            self._data["finished_at"] = _now()
            self._persist()

    def discard(self) -> None:
        """Remove the record. Used when the run completed without failures."""
        with self._lock, contextlib.suppress(FileNotFoundError):
            self._path.unlink()

    def completed_keys(self) -> set[str]:
        result: set[str] = set()
        for item in self._data["plan"]:
            state = self._data["operations"].get(str(item["seq"]))
            if state is not None and state["status"] == OP_COMPLETED:
                result.add(self._plan_key(item))
        return result

    def plan_keys(self) -> set[str]:
        return {self._plan_key(item) for item in self._data["plan"]}

    @staticmethod
    def _plan_key(item: dict[str, Any]) -> str:
        name = canonicalize_name(item["name"])
        if item["job_type"] == "update":
            return (
                f"update:{name}:{item['initial_version']}->{item['version']}"
            )
        return f"{item['job_type']}:{name}:{item['version']}"

    def reconcile(self, env: Env) -> ReconciliationReport:
        entries: list[ReconciliationEntry] = []
        env_checked = True

        for item in self._data["plan"]:
            seq = int(item["seq"])
            state = self._data["operations"].get(str(seq))
            status = state["status"] if state else OP_PENDING
            phase = state.get("phase") if state else None
            error = state.get("error") if state else None
            category: str
            detail: str | None = None

            if status == OP_SKIPPED:
                category = FACT_SKIPPED
                detail = state.get("skip_reason") if state else None
            elif status == OP_PENDING:
                category = FACT_NOT_STARTED
            elif status in {OP_FAILED, OP_INTERRUPTED} or (
                status == OP_RUNNING and self.status != RUN_RUNNING
            ):
                # For an update, once removal of the old version succeeded,
                # any later failure leaves the environment without either
                # version ("removed, but not reinstalled").
                if (
                    item["job_type"] == "update"
                    and phase in {PHASE_OLD_REMOVED, PHASE_INSTALL}
                ):
                    category = FACT_REMOVED_NOT_REINSTALLED
                    detail = (
                        "old version removed, new version was not installed"
                    )
                else:
                    category = FACT_INCOMPLETE
                    parts = [status]
                    if phase:
                        parts.append(f"during {phase}")
                    if error:
                        parts.append(
                            f"{error['type']}: {error['message']}"
                        )
                    detail = ", ".join(parts)
            elif status == OP_RUNNING:
                # Run still in progress (e.g. another process is executing it)
                category = FACT_INCOMPLETE
                detail = "in progress"
            else:  # OP_COMPLETED
                verified = self._verify_item(item, env)
                if verified is True:
                    category = FACT_VERIFIED
                elif verified is False:
                    category = FACT_COMPLETED_NOT_PRESENT
                else:
                    category = FACT_UNCHECKED
                    env_checked = False

            entries.append(
                ReconciliationEntry(
                    seq=seq,
                    plan=item,
                    state=state,
                    category=category,
                    detail=detail,
                )
            )

        return ReconciliationReport(self, entries, env_checked)

    @staticmethod
    def _verify_item(item: dict[str, Any], env: Env) -> bool | None:
        """Verify a completed plan item against the live environment."""
        try:
            dist = env.site_packages.find_distribution(item["name"])
        except Exception:
            logger.debug("Could not query distributions", exc_info=True)
            return None

        if item["job_type"] == "uninstall":
            return dist is None

        if dist is None:
            return False

        try:
            from poetry.core.constraints.version import parse_version

            return parse_version(str(dist.version)) == parse_version(
                item["version"]
            )
        except Exception:  # noqa: BLE001
            return str(dist.version) == item["version"]


class JournalStore:
    """
    Locates installation records. Records are separated first by the
    environment and then by the project they were created for.
    """

    def __init__(
        self,
        root: Path,
        env_path: Path,
        project_path: Path | None = None,
    ) -> None:
        self._root = root
        self._directory = root / self._slug(env_path) / self._slug(project_path)

    @property
    def directory(self) -> Path:
        return self._directory

    @staticmethod
    def _slug(path: Path | None) -> str:
        if path is None:
            return "no-project"

        expanded = path.expanduser()
        resolved = expanded.resolve()
        digest = hashlib.sha1(str(resolved).encode("utf-8")).hexdigest()[:10]
        name = resolved.name or expanded.name or "path"
        name = re.sub(r"[^A-Za-z0-9_.-]+", "-", name).strip("-") or "path"
        return f"{name}-{digest}"

    def create_run(
        self,
        *,
        env: Env,
        project_path: Path | None,
        operations: list[Operation],
        resumed_from: str | None = None,
    ) -> InstallJournal:
        return InstallJournal.create(
            self._directory,
            env=env,
            project_path=project_path,
            operations=operations,
            resumed_from=resumed_from,
        )

    def list_runs(self) -> list[InstallJournal]:
        if not self._directory.is_dir():
            return []

        journals: list[InstallJournal] = []
        for path in sorted(self._directory.glob("*.json"), reverse=True):
            with contextlib.suppress(Exception):
                journals.append(InstallJournal.load(path))
        # Newest run first; run ids sort chronologically.
        journals.sort(key=lambda journal: journal.run_id, reverse=True)
        return journals

    def latest_incomplete_run(self) -> InstallJournal | None:
        for journal in self.list_runs():
            if journal.is_incomplete:
                return journal
        return None


# Re-exported for callers that build plans without importing operations here.
__all__ = [
    "FACT_COMPLETED_NOT_PRESENT",
    "FACT_INCOMPLETE",
    "FACT_NOT_STARTED",
    "FACT_REMOVED_NOT_REINSTALLED",
    "FACT_SKIPPED",
    "FACT_UNCHECKED",
    "FACT_VERIFIED",
    "INCOMPLETE_RUN_STATUSES",
    "OP_COMPLETED",
    "OP_FAILED",
    "OP_INTERRUPTED",
    "OP_PENDING",
    "OP_RUNNING",
    "OP_SKIPPED",
    "PHASE_INSTALL",
    "PHASE_OLD_REMOVED",
    "PHASE_PREPARE",
    "PHASE_REMOVE_OLD",
    "PHASE_UNINSTALL",
    "RUN_COMPLETED",
    "RUN_FAILED",
    "RUN_INTERRUPTED",
    "RUN_RUNNING",
    "InstallJournal",
    "JournalStore",
    "ReconciliationEntry",
    "ReconciliationReport",
    "describe_operation",
    "operation_key",
    "verify_operation",
]
