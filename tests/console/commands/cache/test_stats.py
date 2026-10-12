from __future__ import annotations

import json

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from poetry.utils.cache import FileCache


if TYPE_CHECKING:
    from cleo.testers.command_tester import CommandTester

    from tests.conftest import Config
    from tests.types import CommandTesterFactory


@pytest.fixture
def tester(command_tester_factory: CommandTesterFactory) -> CommandTester:
    return command_tester_factory("cache stats")


@pytest.fixture
def populated_cache(config: Config) -> None:
    artifacts = config.artifacts_cache_directory / "aa" / "bb"
    artifacts.mkdir(parents=True)
    (artifacts / "demo-1.0.whl").write_bytes(b"x" * 1024)

    repositories = config.repository_cache_directory / "PyPI"
    repositories.mkdir(parents=True)
    release_cache: FileCache[object] = FileCache(repositories)
    release_cache.put("demo:1.0", {"name": "demo", "version": "1.0"})


def test_cache_stats(
    tester: CommandTester, populated_cache: None, config: Config
) -> None:
    tester.execute()

    output = tester.io.fetch_output()
    assert "Category" in output
    assert "artifacts" in output
    assert "repositories" in output
    assert "total" in output
    assert "1.0 KiB" in output
    assert "Quarantine: 0 entries (0 B)" in output
    assert str(Path(config.get("cache-dir"))) in output


def test_cache_stats_json(
    tester: CommandTester, populated_cache: None, config: Config
) -> None:
    tester.execute("--json")

    data = json.loads(tester.io.fetch_output())
    categories = {item["category"]: item for item in data["categories"]}
    assert categories["artifacts"]["entries"] == 1
    assert categories["artifacts"]["size"] == 1024
    assert categories["artifacts"]["max_size"] is None
    assert categories["repositories"]["entries"] == 1
    assert data["total"]["entries"] == 2
    assert data["total"]["size"] > 1024
    assert data["cache_dir"] == str(Path(config.get("cache-dir")))


def test_cache_stats_empty(tester: CommandTester) -> None:
    tester.execute()

    output = tester.io.fetch_output()
    assert "artifacts" in output
    assert "repositories" in output
    assert "0 B" in output
