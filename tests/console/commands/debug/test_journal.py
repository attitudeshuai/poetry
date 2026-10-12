from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from poetry.core.packages.package import Package

from poetry.installation.journal import RUN_FAILED
from poetry.installation.journal import JournalStore
from poetry.installation.operations import Install


if TYPE_CHECKING:
    from cleo.testers.command_tester import CommandTester

    from poetry.poetry import Poetry
    from poetry.utils.env import MockEnv
    from tests.types import CommandTesterFactory


@pytest.fixture
def tester(command_tester_factory: CommandTesterFactory) -> CommandTester:
    return command_tester_factory("debug journal")


def test_debug_journal_without_records(tester: CommandTester) -> None:
    tester.execute()

    assert tester.io.fetch_output() == (
        "No installation records found for this project and environment.\n"
    )
    assert tester.status_code == 0


def test_debug_journal_reconciles_latest_incomplete_run(
    tester: CommandTester,
    poetry: Poetry,
    env: MockEnv,
) -> None:
    cache_dir = Path(poetry.config.get("cache-dir"))
    project_path = Path(poetry.file.path).parent
    store = JournalStore(cache_dir / "journal", env.path, project_path)

    operations = [Install(Package("demo", "1.0.0"))]
    run = store.create_run(
        env=env, project_path=project_path, operations=operations
    )
    run.update_operation(
        0,
        status="failed",
        phase="prepare",
        error=RuntimeError("download failed"),
    )
    run.finish(RUN_FAILED)

    tester.execute()
    output = tester.io.fetch_output()

    assert run.run_id in output
    assert RUN_FAILED in output
    assert "Installing demo (1.0.0)" in output
    assert "RuntimeError: download failed" in output
    assert "Facts: 1 incomplete" in output
    assert "poetry install --resume" in output
    assert tester.status_code == 0


def test_debug_journal_all_lists_runs(
    tester: CommandTester,
    poetry: Poetry,
    env: MockEnv,
) -> None:
    cache_dir = Path(poetry.config.get("cache-dir"))
    project_path = Path(poetry.file.path).parent
    store = JournalStore(cache_dir / "journal", env.path, project_path)

    operations = [Install(Package("demo", "1.0.0"))]
    run = store.create_run(
        env=env, project_path=project_path, operations=operations
    )
    run.finish(RUN_FAILED)

    tester.execute("--all")
    output = tester.io.fetch_output()

    assert run.run_id in output
    assert RUN_FAILED in output
