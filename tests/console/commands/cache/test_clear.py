from __future__ import annotations

import os
import time

from typing import TYPE_CHECKING
from typing import TypeVar

import pytest

from cleo.testers.application_tester import ApplicationTester

from poetry.console.application import Application
from poetry.utils.cache import GOVERNANCE_DIRNAME


if TYPE_CHECKING:
    from pathlib import Path

    from poetry.utils.cache import FileCache
    from tests.conftest import Config

T = TypeVar("T")


def _age(path: Path, days: float) -> None:
    timestamp = time.time() - days * 86400
    os.utime(path, (timestamp, timestamp))


@pytest.fixture
def tester() -> ApplicationTester:
    app = Application()

    tester = ApplicationTester(app)
    return tester


@pytest.mark.parametrize("inputs", ["yes", "no"])
def test_cache_clear_all(
    tester: ApplicationTester,
    repository_cache_dir: Path,
    repositories: list[str],
    repository_dirs: list[Path],
    caches: list[FileCache[dict[str, str]]],
    inputs: str,
) -> None:
    exit_code = tester.execute("cache clear --all", inputs=inputs)

    assert exit_code == 0
    assert tester.io.fetch_output() == ""

    if inputs == "yes":
        assert not repository_dirs[0].exists() or not any(repository_dirs[0].iterdir())
        assert not repository_dirs[1].exists() or not any(repository_dirs[1].iterdir())
        assert not caches[0].has("cachy:0.1")
        assert not caches[0].has("cleo:0.2")
        assert not caches[1].has("cachy:0.1")
        assert not caches[1].has("cashy:0.2")
    else:
        assert any((repository_cache_dir / repositories[0]).iterdir())
        assert any((repository_cache_dir / repositories[1]).iterdir())
        assert caches[0].has("cachy:0.1")
        assert caches[0].has("cleo:0.2")
        assert caches[1].has("cachy:0.1")
        assert caches[1].has("cashy:0.2")


@pytest.mark.parametrize("inputs", ["yes", "no"])
def test_cache_clear_all_one_cache(
    tester: ApplicationTester,
    repository_cache_dir: Path,
    repositories: list[str],
    repository_dirs: list[Path],
    caches: list[FileCache[dict[str, str]]],
    inputs: str,
) -> None:
    exit_code = tester.execute(f"cache clear {repositories[0]} --all", inputs=inputs)

    assert exit_code == 0
    assert tester.io.fetch_output() == ""

    if inputs == "yes":
        assert not repository_dirs[0].exists() or not any(repository_dirs[0].iterdir())
        assert not caches[0].has("cachy:0.1")
        assert not caches[0].has("cleo:0.2")
    else:
        assert any((repository_cache_dir / repositories[0]).iterdir())
        assert caches[0].has("cachy:0.1")
        assert caches[0].has("cleo:0.2")

    assert any((repository_cache_dir / repositories[1]).iterdir())
    assert caches[1].has("cachy:0.1")
    assert caches[1].has("cashy:0.2")


def test_cache_clear_all_no_entries(tester: ApplicationTester) -> None:
    exit_code = tester.execute("cache clear --all")

    assert exit_code == 0
    assert tester.io.fetch_output().strip() == "No cache entries"


def test_cache_clear_all_one_cache_no_entries(
    tester: ApplicationTester,
    repository_cache_dir: Path,
    repositories: list[str],
) -> None:
    exit_code = tester.execute(f"cache clear {repositories[0]} --all")

    assert exit_code == 0

    assert tester.io.fetch_output().strip() == f"No cache entries for {repositories[0]}"


@pytest.mark.parametrize("with_repo", [False, True])
def test_cache_clear_missing_option(
    tester: ApplicationTester, repositories: list[str], with_repo: bool
) -> None:
    command = f"cache clear {repositories[0]}" if with_repo else "cache clear"
    exit_code = tester.execute(command)

    assert exit_code == 1
    assert (
        "Add the --all option if you want to clear all cache entries"
        in tester.io.fetch_error()
    )


@pytest.mark.parametrize("inputs", ["yes", "no"])
@pytest.mark.parametrize("package_name", ["cachy", "Cachy"])
def test_cache_clear_pkg(
    tester: ApplicationTester,
    repositories: list[str],
    caches: list[FileCache[dict[str, str]]],
    package_name: str,
    inputs: str,
) -> None:
    exit_code = tester.execute(
        f"cache clear {repositories[1]}:{package_name}:0.1", inputs=inputs
    )

    assert exit_code == 0
    assert tester.io.fetch_output() == ""

    if inputs == "yes":
        assert not caches[1].has("cachy:0.1")
        assert caches[1].has("cashy:0.2")
    else:
        assert caches[1].has("cachy:0.1")
        assert caches[1].has("cashy:0.2")

    assert caches[0].has("cachy:0.1")


# --------------------------------------------------------------------------- #
# Selective, category-aware cache clearing
# --------------------------------------------------------------------------- #


def _seed_category_files(config: Config) -> dict[str, Path]:
    artifacts = config.artifacts_cache_directory
    artifacts.mkdir(parents=True, exist_ok=True)
    old_archive = artifacts / "old.whl"
    new_archive = artifacts / "new.whl"
    big_archive = artifacts / "big.whl"
    old_archive.write_bytes(b"o" * 200)
    new_archive.write_bytes(b"n" * 200)
    big_archive.write_bytes(b"b" * 5000)
    _age(old_archive, 60)
    _age(big_archive, 60)

    repositories = config.repository_cache_directory / "PyPI"
    repositories.mkdir(parents=True, exist_ok=True)
    repository_entry = repositories / "metadata.bin"
    repository_entry.write_bytes(b"m" * 200)

    return {
        "old": old_archive,
        "new": new_archive,
        "big": big_archive,
        "repo": repository_entry,
    }


def test_cache_clear_artifacts_dry_run(
    tester: ApplicationTester, config: Config
) -> None:
    files = _seed_category_files(config)

    exit_code = tester.execute("cache clear --all --category artifacts --dry-run")

    assert exit_code == 0
    output = tester.io.fetch_output()
    assert "Would delete 3 entries" in output
    assert files["old"].exists()
    assert files["new"].exists()
    assert files["big"].exists()
    assert files["repo"].exists()


def test_cache_clear_artifacts(tester: ApplicationTester, config: Config) -> None:
    files = _seed_category_files(config)

    exit_code = tester.execute("cache clear --all --category artifacts", inputs="yes")

    assert exit_code == 0
    assert "Deleted 3 entries" in tester.io.fetch_output()
    assert not files["old"].exists()
    assert not files["new"].exists()
    assert not files["big"].exists()
    # the repository category is untouched
    assert files["repo"].exists()
    # governance records are retained
    governance = config.artifacts_cache_directory / GOVERNANCE_DIRNAME
    assert governance.exists()
    assert (governance / "events.jsonl").exists()


def test_cache_clear_artifacts_declined(
    tester: ApplicationTester, config: Config
) -> None:
    files = _seed_category_files(config)

    exit_code = tester.execute("cache clear --all --category artifacts", inputs="no")

    assert exit_code == 0
    assert files["old"].exists()
    assert files["new"].exists()
    assert files["big"].exists()


def test_cache_clear_older_than(tester: ApplicationTester, config: Config) -> None:
    files = _seed_category_files(config)

    exit_code = tester.execute("cache clear --all --older-than 30d", inputs="yes")

    assert exit_code == 0
    assert "Deleted 2 entries" in tester.io.fetch_output()
    assert not files["old"].exists()
    assert not files["big"].exists()
    assert files["new"].exists()
    assert files["repo"].exists()


def test_cache_clear_larger_than(tester: ApplicationTester, config: Config) -> None:
    files = _seed_category_files(config)

    exit_code = tester.execute("cache clear --all --larger-than 1KiB", inputs="yes")

    assert exit_code == 0
    assert not files["big"].exists()
    assert files["old"].exists()
    assert files["new"].exists()
    assert files["repo"].exists()


def test_cache_clear_shrink_to(tester: ApplicationTester, config: Config) -> None:
    files = _seed_category_files(config)
    # only keep the two 200B entries, one old and one recently used
    files["big"].unlink()

    exit_code = tester.execute(
        "cache clear --all --category artifacts --shrink-to 250", inputs="yes"
    )

    assert exit_code == 0
    output = tester.io.fetch_output()
    assert "Deleted 1 entries" in output
    assert files["new"].exists() ^ files["old"].exists()


def test_cache_clear_selectors_no_match(
    tester: ApplicationTester, config: Config
) -> None:
    _seed_category_files(config)

    exit_code = tester.execute("cache clear --all --older-than 3650d")

    assert exit_code == 0
    assert "No cache entries matched the given criteria" in tester.io.fetch_output()


def test_cache_clear_selective_requires_all(tester: ApplicationTester) -> None:
    exit_code = tester.execute("cache clear --older-than 30d")

    assert exit_code == 1
    assert (
        "Add the --all option if you want to clear all cache entries"
        in tester.io.fetch_error()
    )


def test_cache_clear_selective_rejects_repository_argument(
    tester: ApplicationTester,
) -> None:
    exit_code = tester.execute("cache clear PyPI --all --dry-run")

    assert exit_code == 1
    assert "cannot be combined" in tester.io.fetch_error()


def test_cache_clear_unknown_category(tester: ApplicationTester) -> None:
    exit_code = tester.execute("cache clear --all --category unknown")

    assert exit_code == 1
    assert "Unknown cache category" in tester.io.fetch_error()


def test_cache_clear_invalid_size(tester: ApplicationTester) -> None:
    exit_code = tester.execute("cache clear --all --larger-than huge")

    assert exit_code == 1
    assert "Invalid size" in tester.io.fetch_error()
