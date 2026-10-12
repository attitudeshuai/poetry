from __future__ import annotations

import json

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from cleo.formatters.style import Style
from cleo.io.buffered_io import BufferedIO
from poetry.core.packages.package import Package

from poetry.installation.executor import Executor
from poetry.installation.journal import FACT_COMPLETED_NOT_PRESENT
from poetry.installation.journal import FACT_INCOMPLETE
from poetry.installation.journal import FACT_NOT_STARTED
from poetry.installation.journal import FACT_REMOVED_NOT_REINSTALLED
from poetry.installation.journal import FACT_VERIFIED
from poetry.installation.journal import OP_COMPLETED
from poetry.installation.journal import OP_FAILED
from poetry.installation.journal import OP_PENDING
from poetry.installation.journal import OP_SKIPPED
from poetry.installation.journal import PHASE_INSTALL
from poetry.installation.journal import PHASE_OLD_REMOVED
from poetry.installation.journal import RUN_COMPLETED
from poetry.installation.journal import RUN_FAILED
from poetry.installation.journal import RUN_INTERRUPTED
from poetry.installation.journal import InstallJournal
from poetry.installation.journal import JournalStore
from poetry.installation.journal import operation_key
from poetry.installation.journal import verify_operation
from poetry.installation.operations import Install
from poetry.installation.operations import Uninstall
from poetry.installation.operations import Update
from poetry.installation.wheel_installer import WheelInstaller
from poetry.repositories.repository_pool import RepositoryPool
from poetry.utils.env import MockEnv


if TYPE_CHECKING:
    from pytest_mock import MockerFixture

    from poetry.config.config import Config
    from poetry.installation.operations.operation import Operation


@pytest.fixture
def env(tmp_path: Path) -> MockEnv:
    path = tmp_path / ".venv"
    path.mkdir(parents=True)
    return MockEnv(path=path, is_venv=True)


@pytest.fixture
def io() -> BufferedIO:
    io = BufferedIO()
    io.output.formatter.set_style("warning", Style("yellow"))
    return io


@pytest.fixture
def pool() -> RepositoryPool:
    return RepositoryPool()


@pytest.fixture
def store(config: Config, tmp_path: Path, env: MockEnv) -> JournalStore:
    config.merge({"cache-dir": str(tmp_path / "cache")})
    return JournalStore(
        Path(config.get("cache-dir")) / "journal",
        env.path,
        tmp_path / "project",
    )


def make_dist_info(env: MockEnv, name: str, version: str) -> Path:
    site_packages = env.path / "purelib"
    site_packages.mkdir(parents=True, exist_ok=True)
    dist_info = site_packages / f"{name.replace('-', '_')}-{version}.dist-info"
    dist_info.mkdir(parents=True, exist_ok=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        encoding="utf-8",
    )
    return dist_info


def test_store_separates_environments_and_projects(
    tmp_path: Path,
) -> None:
    root = tmp_path / "journal"
    env_a = tmp_path / "venv-a"
    env_b = tmp_path / "venv-b"
    project_a = tmp_path / "project-a"
    project_b = tmp_path / "project-b"

    store_a1 = JournalStore(root, env_a, project_a)
    store_a2 = JournalStore(root, env_a, project_a)
    store_b = JournalStore(root, env_b, project_a)
    store_c = JournalStore(root, env_a, project_b)

    # Deterministic and stable for the same env/project pair
    assert store_a1.directory == store_a2.directory
    assert store_a1.directory != store_b.directory
    assert store_a1.directory != store_c.directory
    assert store_b.directory != store_c.directory


def test_concurrent_runs_keep_independent_records(
    store: JournalStore, env: MockEnv
) -> None:
    operations = [Install(Package("demo", "1.0.0"))]

    run_one = store.create_run(
        env=env, project_path=None, operations=operations
    )
    # Same batch started again concurrently gets a different file.
    run_two = store.create_run(
        env=env, project_path=None, operations=operations
    )

    assert run_one.path != run_two.path
    assert run_one.path.exists()
    assert run_two.path.exists()

    run_one.update_operation(0, status=OP_COMPLETED)
    run_two.update_operation(0, status=OP_FAILED, error=RuntimeError("boom"))

    reloaded_one = InstallJournal.load(run_one.path)
    reloaded_two = InstallJournal.load(run_two.path)

    assert reloaded_one.state(0)["status"] == OP_COMPLETED
    assert reloaded_two.state(0)["status"] == OP_FAILED
    assert reloaded_two.state(0)["error"] == {
        "type": "RuntimeError",
        "message": "boom",
    }


def test_run_persists_plan_and_all_states(
    store: JournalStore, env: MockEnv
) -> None:
    operations = [
        Install(Package("demo", "1.0.0")),
        Uninstall(Package("attrs", "17.4.0")).skip("Not currently installed"),
        Update(Package("requests", "2.18.3"), Package("requests", "2.18.4")),
    ]

    run = store.create_run(env=env, project_path=None, operations=operations)

    raw = json.loads(run.path.read_text(encoding="utf-8"))
    assert raw["status"] == "running"
    assert raw["environment"]["path"] == str(env.path)
    assert [item["job_type"] for item in raw["plan"]] == [
        "install",
        "uninstall",
        "update",
    ]
    assert raw["plan"][1]["skipped"] is True
    assert raw["operations"]["0"]["status"] == OP_PENDING
    assert raw["operations"]["1"]["status"] == OP_SKIPPED

    run.update_operation(0, status=OP_COMPLETED)
    run.update_operation(2, status=OP_FAILED, error=RuntimeError("nope"))
    run.finish(RUN_FAILED)

    reloaded = InstallJournal.load(run.path)
    assert reloaded.status == RUN_FAILED
    assert reloaded.finished_at is not None
    assert reloaded.state(0)["status"] == OP_COMPLETED
    assert reloaded.state(2)["status"] == OP_FAILED


def test_finish_marks_abandoned_running_operations_as_interrupted(
    store: JournalStore, env: MockEnv
) -> None:
    run = store.create_run(
        env=env,
        project_path=None,
        operations=[Install(Package("demo", "1.0.0"))],
    )
    run.update_operation(0, status="running")
    # Simulate a killed process: finish() is called by a later inspection.
    run.finish(RUN_INTERRUPTED)

    assert run.state(0)["status"] == "interrupted"


def test_discard_removes_the_record(store: JournalStore, env: MockEnv) -> None:
    run = store.create_run(
        env=env,
        project_path=None,
        operations=[Install(Package("demo", "1.0.0"))],
    )
    assert run.path.exists()
    run.discard()
    assert not run.path.exists()


def test_latest_incomplete_run_only_returns_incomplete(
    store: JournalStore, env: MockEnv
) -> None:
    operations = [Install(Package("demo", "1.0.0"))]

    assert store.latest_incomplete_run() is None

    failed = store.create_run(env=env, project_path=None, operations=operations)
    failed.finish(RUN_FAILED)
    assert store.latest_incomplete_run().path == failed.path

    failed.discard()
    assert store.latest_incomplete_run() is None

    completed = store.create_run(env=env, project_path=None, operations=operations)
    completed.finish(RUN_COMPLETED)
    assert store.latest_incomplete_run() is None


def test_verify_operation_against_environment(env: MockEnv) -> None:
    install = Install(Package("demo", "1.0.0"))
    update = Update(Package("demo", "1.0.0"), Package("demo", "2.0.0"))
    uninstall = Uninstall(Package("demo", "1.0.0"))

    assert verify_operation(install, env) is False
    assert verify_operation(update, env) is False
    assert verify_operation(uninstall, env) is True

    make_dist_info(env, "demo", "1.0.0")

    assert verify_operation(install, env) is True
    assert verify_operation(update, env) is False
    assert verify_operation(uninstall, env) is False

    # Simulate the finished update: only the new version is present.
    (env.path / "purelib" / "demo-1.0.0.dist-info").rename(
        env.path / "purelib" / "demo-2.0.0.dist-info"
    )
    (env.path / "purelib" / "demo-2.0.0.dist-info" / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: demo\nVersion: 2.0.0\n",
        encoding="utf-8",
    )
    assert verify_operation(update, env) is True


def test_operation_key_is_stable_and_distinct() -> None:
    assert operation_key(Install(Package("Demo", "1.0.0"))) == "install:demo:1.0.0"
    assert (
        operation_key(
            Update(Package("demo", "1.0.0"), Package("demo", "2.0.0"))
        )
        == "update:demo:1.0.0->2.0.0"
    )
    assert (
        operation_key(Uninstall(Package("demo", "1.0.0")))
        == "uninstall:demo:1.0.0"
    )


def test_reconcile_reports_only_facts(
    store: JournalStore, env: MockEnv
) -> None:
    install_done = Install(Package("alpha", "1.0.0"))
    uninstall_done = Uninstall(Package("gone", "1.0.0"))
    install_missing = Install(Package("beta", "1.0.0"))
    half_update = Update(
        Package("gamma", "1.0.0"), Package("gamma", "2.0.0")
    )
    failed_install = Install(Package("delta", "1.0.0"))
    not_started = Install(Package("epsilon", "1.0.0"))
    operations = [
        install_done,
        uninstall_done,
        install_missing,
        half_update,
        failed_install,
        not_started,
    ]

    run = store.create_run(env=env, project_path=None, operations=operations)

    # alpha was installed and is really in the environment
    make_dist_info(env, "alpha", "1.0.0")
    run.update_operation(0, status=OP_COMPLETED)
    # gone was uninstalled and is absent
    run.update_operation(1, status=OP_COMPLETED)
    # beta recorded as completed but the file is gone
    run.update_operation(2, status=OP_COMPLETED)
    # gamma: old version removed; the new wheel install then failed
    run.update_operation(3, phase="remove-old")
    run.update_operation(3, phase=PHASE_OLD_REMOVED)
    run.update_operation(3, phase=PHASE_INSTALL)
    run.update_operation(3, status=OP_FAILED, error=RuntimeError("x"))
    # delta failed while downloading
    run.update_operation(
        4, status=OP_FAILED, phase="prepare", error=RuntimeError("y")
    )
    # epsilon never started
    run.finish(RUN_FAILED)

    report = run.reconcile(env)
    categories = {entry.seq: entry.category for entry in report.entries}

    assert categories[0] == FACT_VERIFIED
    assert categories[1] == FACT_VERIFIED
    assert categories[2] == FACT_COMPLETED_NOT_PRESENT
    assert categories[3] == FACT_REMOVED_NOT_REINSTALLED
    assert categories[4] == FACT_INCOMPLETE
    assert categories[5] == FACT_NOT_STARTED

    pending = report.render_pending()
    assert "Updating gamma (1.0.0 -> 2.0.0)" in pending
    assert "old version removed, new version was not installed" in pending
    assert "Installing delta (1.0.0)" in pending
    assert "RuntimeError: y" in pending
    assert "Installing epsilon (1.0.0): not started" in pending
    # Facts only: no rollback instructions
    assert "rollback" not in pending.lower()


# ---------------------------------------------------------------------------
# Executor integration
# ---------------------------------------------------------------------------


def make_executor(
    config: Config,
    pool: RepositoryPool,
    io: BufferedIO,
    env: MockEnv,
    tmp_path: Path,
    *,
    resume: bool = False,
) -> Executor:
    config.merge({"cache-dir": str(tmp_path / "cache")})
    return Executor(
        env,
        pool,
        config,
        io,
        parallel=False,
        project_path=tmp_path / "project",
        resume=resume,
    )


def patch_downloads(mocker: MockerFixture) -> None:
    mocker.patch.object(
        Executor,
        "_download",
        return_value=Path("demo-0.1.0-py2.py3-none-any.whl"),
    )


def test_executor_success_leaves_no_record(
    mocker: MockerFixture,
    config: Config,
    pool: RepositoryPool,
    io: BufferedIO,
    env: MockEnv,
    tmp_path: Path,
) -> None:
    patch_downloads(mocker)
    install_wheel = mocker.patch.object(WheelInstaller, "install")

    executor = make_executor(config, pool, io, env, tmp_path)
    return_code = executor.execute(
        [
            Install(Package("alpha", "1.0.0")),
            Install(Package("beta", "1.0.0")),
        ]
    )

    assert return_code == 0
    assert install_wheel.call_count == 2
    journal_dir = Path(config.get("cache-dir")) / "journal"
    assert not list(journal_dir.rglob("*.json"))
    assert io.fetch_error() == ""


def test_executor_failure_persists_record_and_reports_facts(
    mocker: MockerFixture,
    config: Config,
    pool: RepositoryPool,
    io: BufferedIO,
    env: MockEnv,
    tmp_path: Path,
) -> None:
    patch_downloads(mocker)

    # First wheel install succeeds, the second one fails.
    mocker.patch.object(
        WheelInstaller,
        "install",
        side_effect=[None, RuntimeError("install blew up")],
    )

    executor = make_executor(config, pool, io, env, tmp_path)
    return_code = executor.execute(
        [
            Install(Package("alpha", "1.0.0")),
            Install(Package("beta", "1.0.0")),
        ]
    )

    assert return_code == 1

    records = list((Path(config.get("cache-dir")) / "journal").rglob("*.json"))
    assert len(records) == 1
    raw = json.loads(records[0].read_text(encoding="utf-8"))
    assert raw["status"] == RUN_FAILED
    states = raw["operations"]
    assert states["0"]["status"] == OP_COMPLETED
    assert states["1"]["status"] == OP_FAILED
    assert states["1"]["error"]["type"] == "RuntimeError"

    error_output = io.fetch_error()
    assert "Installation did not complete" in error_output
    assert "Installing beta (1.0.0)" in error_output
    assert "--resume" in error_output
    assert records[0].name in error_output


def test_executor_records_update_as_removed_not_reinstalled(
    mocker: MockerFixture,
    config: Config,
    pool: RepositoryPool,
    io: BufferedIO,
    env: MockEnv,
    tmp_path: Path,
) -> None:
    patch_downloads(mocker)

    def install(self: WheelInstaller, archive: Path) -> None:
        raise RuntimeError("install blew up")

    mocker.patch.object(WheelInstaller, "install", install)

    executor = make_executor(config, pool, io, env, tmp_path)
    operation = Update(
        Package("gamma", "1.0.0"), Package("gamma", "2.0.0")
    )
    assert executor.execute([operation]) == 1

    records = list((Path(config.get("cache-dir")) / "journal").rglob("*.json"))
    raw = json.loads(records[0].read_text(encoding="utf-8"))
    state = raw["operations"]["0"]
    assert state["status"] == OP_FAILED
    # The old version was removed before the new wheel install failed.
    assert state["phase"] == PHASE_INSTALL

    journal = InstallJournal.load(records[0])
    report = journal.reconcile(env)
    assert report.entries[0].category == FACT_REMOVED_NOT_REINSTALLED
    assert (
        "old version removed, new version was not installed"
        in report.render_pending()
    )


def test_executor_interrupt_marks_run_interrupted(
    mocker: MockerFixture,
    config: Config,
    pool: RepositoryPool,
    io: BufferedIO,
    env: MockEnv,
    tmp_path: Path,
) -> None:
    # A return value of -2 means KeyboardInterrupt in a pip subprocess.
    mocker.patch.object(Executor, "_install", return_value=-2)

    executor = make_executor(config, pool, io, env, tmp_path)
    assert executor.execute([Install(Package("alpha", "1.0.0"))]) == 1

    records = list((Path(config.get("cache-dir")) / "journal").rglob("*.json"))
    assert len(records) == 1
    raw = json.loads(records[0].read_text(encoding="utf-8"))
    assert raw["status"] == RUN_INTERRUPTED
    error_output = io.fetch_error()
    assert "cancelled" in error_output.lower()
    assert "--resume" in error_output


def test_executor_resume_skips_verified_operations(
    mocker: MockerFixture,
    config: Config,
    pool: RepositoryPool,
    io: BufferedIO,
    env: MockEnv,
    tmp_path: Path,
) -> None:
    # Track the package currently being installed via _download.
    downloaded: list[str] = []

    def fake_download(operation: Operation) -> Path:
        downloaded.append(operation.package.name)
        return Path("demo-0.1.0-py2.py3-none-any.whl")

    mocker.patch.object(Executor, "_download", side_effect=fake_download)

    state = {"run": "1"}
    calls_by_run: dict[str, list[str]] = {"1": [], "2": []}

    def wheel_install(archive: Path) -> None:
        name = downloaded[-1]
        calls_by_run[state["run"]].append(name)
        if state["run"] == "1" and name == "beta":
            raise RuntimeError("install blew up")

    install_mock = mocker.patch.object(
        WheelInstaller, "install", side_effect=wheel_install
    )

    # First run: alpha is installed, beta fails during its install.
    executor = make_executor(config, pool, io, env, tmp_path)
    assert (
        executor.execute(
            [
                Install(Package("alpha", "1.0.0")),
                Install(Package("beta", "1.0.0")),
            ]
        )
        == 1
    )
    assert calls_by_run["1"] == ["alpha", "beta"]

    # The environment now really contains alpha.
    make_dist_info(env, "alpha", "1.0.0")

    # Second run with --resume: alpha must not be installed again.
    downloaded.clear()
    state["run"] = "2"
    resume_io = BufferedIO()
    resume_io.output.formatter.set_style("warning", Style("yellow"))
    resumed = make_executor(config, pool, resume_io, env, tmp_path, resume=True)

    assert (
        resumed.execute(
            [
                Install(Package("alpha", "1.0.0")),
                Install(Package("beta", "1.0.0")),
            ]
        )
        == 0
    )

    assert downloaded == ["beta"]
    assert calls_by_run["2"] == ["beta"]
    assert install_mock.call_count == 3

    # Both the failed record and the resumed record are gone.
    journal_dir = Path(config.get("cache-dir")) / "journal"
    assert not list(journal_dir.rglob("*.json"))
    assert "--resume" not in resume_io.fetch_error()


def test_executor_resume_redoes_operation_missing_from_environment(
    mocker: MockerFixture,
    config: Config,
    pool: RepositoryPool,
    io: BufferedIO,
    env: MockEnv,
    tmp_path: Path,
) -> None:
    patch_downloads(mocker)
    wheel_install = mocker.patch.object(WheelInstaller, "install")

    # A record claims completion, but the environment does not contain alpha.
    store = JournalStore(
        Path(config.get("cache-dir")) / "journal",
        env.path,
        tmp_path / "project",
    )
    stale = store.create_run(
        env=env,
        project_path=tmp_path / "project",
        operations=[Install(Package("alpha", "1.0.0"))],
    )
    stale.update_operation(0, status=OP_COMPLETED)
    stale.finish(RUN_FAILED)

    resumed = make_executor(config, pool, io, env, tmp_path, resume=True)
    assert resumed.execute([Install(Package("alpha", "1.0.0"))]) == 0
    # alpha is not in the environment, so it is installed again.
    assert wheel_install.call_count == 1


def test_executor_dry_run_and_disabled_do_not_record(
    mocker: MockerFixture,
    config: Config,
    pool: RepositoryPool,
    io: BufferedIO,
    env: MockEnv,
    tmp_path: Path,
) -> None:
    config.merge({"cache-dir": str(tmp_path / "cache")})

    dry_run = Executor(env, pool, config, io, parallel=False, project_path=None)
    dry_run.dry_run(True)
    assert dry_run.execute([Install(Package("alpha", "1.0.0"))]) == 0

    disabled = Executor(env, pool, config, io, parallel=False, project_path=None)
    disabled.disable()
    assert disabled.execute([Install(Package("alpha", "1.0.0"))]) == 0

    journal_dir = Path(config.get("cache-dir")) / "journal"
    assert not journal_dir.exists() or not list(
        journal_dir.rglob("*.json")
    )
