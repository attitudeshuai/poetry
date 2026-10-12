from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from typing import ClassVar

from cleo.helpers import option

from poetry.console.commands.env_command import EnvCommand
from poetry.installation.journal import FACT_COMPLETED_NOT_PRESENT
from poetry.installation.journal import FACT_INCOMPLETE
from poetry.installation.journal import FACT_NOT_STARTED
from poetry.installation.journal import FACT_REMOVED_NOT_REINSTALLED
from poetry.installation.journal import FACT_SKIPPED
from poetry.installation.journal import FACT_UNCHECKED
from poetry.installation.journal import FACT_VERIFIED
from poetry.installation.journal import JournalStore
from poetry.installation.journal import ReconciliationReport


if TYPE_CHECKING:
    from cleo.io.inputs.option import Option


class DebugJournalCommand(EnvCommand):
    name = "debug journal"
    description = (
        "Shows the facts recorded by the last interrupted or failed"
        " installation for this project and environment."
    )

    options: ClassVar[list[Option]] = [
        option(
            "all",
            None,
            "List every recorded installation run instead of reconciling the"
            " latest incomplete one.",
            flag=True,
        ),
    ]

    help = """\
The <info>debug journal</info> command reads the persistent installation record
of the current project and environment.

By default it reconciles the latest run that did not complete: it states which
operations are verified in the environment, which package was removed but not
reinstalled, which operations failed or were interrupted, and which were not
started. The command reports facts only; it never modifies or rolls back the
environment.

Use <info>poetry install --resume</info> (or <info>poetry sync --resume</info>) to
continue from the last completed operation.
"""

    def handle(self) -> int:
        cache_dir = Path(self.poetry.config.get("cache-dir")).expanduser()
        project_path = Path(self.poetry.file.path).parent
        store = JournalStore(cache_dir / "journal", self.env.path, project_path)

        runs = store.list_runs()
        if not runs:
            self.line(
                "No installation records found for this project and"
                " environment."
            )
            return 0

        if self.option("all"):
            self.line(
                f"Installation records in <comment>{store.directory}</>:\n"
            )
            for run in runs:
                self.line(f"  - {run.run_id} [{run.status}]")
            return 0

        run = next((candidate for candidate in runs if candidate.is_incomplete), None)
        if run is None:
            self.line(
                "No incomplete installation records found for this project"
                " and environment."
            )
            return 0

        report = run.reconcile(self.env)

        self.line("")
        self.line(
            f"Installation record: <comment>{run.path}</>"
        )
        self.line(
            f"Run <info>{run.run_id}</> started at {run.started_at},"
            f" status: <fg=yellow>{run.status}</>"
        )
        self.line("")

        rendered = report.render()
        if rendered:
            self.line(rendered)
            self.line("")

        counts = self._counts(report)
        summary = ", ".join(
            f"{counts[fact]} {label}"
            for fact, label in (
                (FACT_VERIFIED, "completed and verified"),
                (FACT_REMOVED_NOT_REINSTALLED, "removed without reinstall"),
                (FACT_INCOMPLETE, "incomplete"),
                (FACT_NOT_STARTED, "not started"),
                (FACT_COMPLETED_NOT_PRESENT, "recorded but missing"),
                (FACT_UNCHECKED, "unverified"),
                (FACT_SKIPPED, "skipped"),
            )
            if counts[fact]
        )
        self.line(f"Facts: {summary}")
        self.line("")
        self.line(
            "Continue with <info>poetry install --resume</> to execute only"
            " the remaining operations."
        )

        return 0

    @staticmethod
    def _counts(report: ReconciliationReport) -> dict[str, int]:
        result = {
            FACT_VERIFIED: 0,
            FACT_SKIPPED: 0,
            FACT_COMPLETED_NOT_PRESENT: 0,
            FACT_REMOVED_NOT_REINSTALLED: 0,
            FACT_INCOMPLETE: 0,
            FACT_NOT_STARTED: 0,
            FACT_UNCHECKED: 0,
        }
        for entry in report.entries:
            result[entry.category] += 1
        return result
