from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import time
import traceback

from pathlib import Path
from typing import TYPE_CHECKING
from typing import TypeVar

import pytest

from packaging.tags import Tag
from poetry.core.packages.utils.link import Link

from poetry.utils.cache import GOVERNANCE_DIRNAME
from poetry.utils.cache import ArtifactCache
from poetry.utils.cache import CacheGovernor
from poetry.utils.cache import EvictionPolicy
from poetry.utils.cache import FileCache
from poetry.utils.cache import format_size
from poetry.utils.cache import parse_duration
from poetry.utils.cache import parse_size
from poetry.utils.env import MockEnv


if TYPE_CHECKING:
    from typing import Any

    from pytest_mock import MockerFixture

    from tests.conftest import Config
    from tests.types import FixtureDirGetter


T = TypeVar("T")


def _set_mtime(path: Path, age_seconds: float) -> None:
    timestamp = time.time() - age_seconds
    os.utime(path, (timestamp, timestamp))


@pytest.fixture
def repository_cache_dir(config: Config) -> Path:
    return config.repository_cache_directory


@pytest.fixture
def poetry_file_cache(repository_cache_dir: Path) -> FileCache[Any]:
    return FileCache(repository_cache_dir / "cache")


def test_cache_validates(repository_cache_dir: Path) -> None:
    with pytest.raises(ValueError) as e:
        FileCache(repository_cache_dir / "cache", hash_type="unknown")
    assert str(e.value) == "FileCache.hash_type is unknown value: 'unknown'."


def test_cache_get_put_has(repository_cache_dir: Path) -> None:
    cache: FileCache[Any] = FileCache(repository_cache_dir / "cache")
    cache.put("key1", "value")
    cache.put("key2", {"a": ["json-encoded", "value"]})

    assert cache.get("key1") == "value"
    assert cache.get("key2") == {"a": ["json-encoded", "value"]}
    assert cache.has("key1")
    assert cache.has("key2")
    assert not cache.has("key3")


def test_cache_forget(repository_cache_dir: Path) -> None:
    cache: FileCache[Any] = FileCache(repository_cache_dir / "cache")
    cache.put("key1", "value")
    cache.put("key2", "value")

    assert cache.has("key1")
    assert cache.has("key2")

    cache.forget("key1")

    assert not cache.has("key1")
    assert cache.has("key2")


def test_cache_flush(repository_cache_dir: Path) -> None:
    cache: FileCache[Any] = FileCache(repository_cache_dir / "cache")
    cache.put("key1", "value")
    cache.put("key2", "value")

    assert cache.has("key1")
    assert cache.has("key2")

    cache.flush()

    assert not cache.has("key1")
    assert not cache.has("key2")


def test_cache_remember(repository_cache_dir: Path, mocker: MockerFixture) -> None:
    cache: FileCache[Any] = FileCache(repository_cache_dir / "cache")

    method = mocker.Mock(return_value="value2")
    cache.put("key1", "value1")
    assert cache.remember("key1", method) == "value1"
    method.assert_not_called()

    assert cache.remember("key2", method) == "value2"
    method.assert_called()


def test_cache_get_limited_minutes(
    repository_cache_dir: Path, mocker: MockerFixture
) -> None:
    cache: FileCache[Any] = FileCache(repository_cache_dir / "cache")

    start_time = 1111111111

    mocker.patch("time.time", return_value=start_time)
    cache.put("key1", "value", minutes=5)
    cache.put("key2", "value", minutes=5)

    assert cache.get("key1") is not None
    assert cache.get("key2") is not None

    mocker.patch("time.time", return_value=start_time + 5 * 60 + 1)
    # check to make sure that the cache deletes for has() and get()
    assert not cache.has("key1")
    assert cache.get("key2") is None


def test_missing_cache_file(poetry_file_cache: FileCache[Any]) -> None:
    poetry_file_cache.put("key1", "value")

    key1_path = (
        poetry_file_cache.path
        / "81/74/09/96/87/a2/66/21/8174099687a26621f4e2cdd7cc03b3dacedb3fb962255b1aafd033cabe831530"
    )
    assert key1_path.exists()
    key1_path.unlink()  # corrupt cache by removing a key file

    assert poetry_file_cache.get("key1") is None


def test_missing_cache_path(poetry_file_cache: FileCache[Any]) -> None:
    poetry_file_cache.put("key1", "value")

    key1_partial_path = poetry_file_cache.path / "81/74/09/96/87/a2/"
    assert key1_partial_path.exists()
    shutil.rmtree(
        key1_partial_path
    )  # corrupt cache by removing a subdirectory containing a key file

    assert poetry_file_cache.get("key1") is None


@pytest.mark.parametrize(
    "corrupt_payload",
    [
        "",  # empty file
        b"\x00",  # null
        "99999999",  # truncated file
        '999999a999"value"',  # corrupt lifetime
        b'9999999999"va\xd8\x00"',  # invalid unicode
        "fil3systemFa!led",  # garbage file
    ],
)
def test_detect_corrupted_cache_key_file(
    corrupt_payload: str | bytes, poetry_file_cache: FileCache[Any]
) -> None:
    poetry_file_cache.put("key1", "value")

    key1_path = (
        poetry_file_cache.path
        / "81/74/09/96/87/a2/66/21/8174099687a26621f4e2cdd7cc03b3dacedb3fb962255b1aafd033cabe831530"
    )
    assert key1_path.exists()

    # original content: 9999999999"value"

    if isinstance(corrupt_payload, str):
        with open(key1_path, "w", encoding="utf-8") as f:
            f.write(corrupt_payload)  # write corrupt data
    else:
        with open(key1_path, "wb") as f:
            f.write(corrupt_payload)  # write corrupt data

    assert poetry_file_cache.get("key1") is None


def test_get_cache_directory_for_link(tmp_path: Path) -> None:
    cache = ArtifactCache(cache_dir=tmp_path)
    directory = cache.get_cache_directory_for_link(
        Link("https://files.pythonhosted.org/poetry-1.1.0.tar.gz")
    )

    expected = Path(
        f"{tmp_path.as_posix()}/41/9c/6e/"
        "ef83f08fcf4dac7cd78d843e7974d601a19c90e4bb90bb76b4a7a61548"
    )

    assert directory == expected


@pytest.mark.parametrize("subdirectory", [None, "subdir"])
def test_get_cache_directory_for_git(tmp_path: Path, subdirectory: str | None) -> None:
    cache = ArtifactCache(cache_dir=tmp_path)
    directory = cache.get_cache_directory_for_git(
        url="https://github.com/demo/demo.git", ref="123456", subdirectory=subdirectory
    )

    if subdirectory:
        expected = Path(
            f"{tmp_path.as_posix()}/53/08/33/"
            "7851e5806669aa15ab0c555b13bd5523978057323c6a23a9cee18ec51c"
        )
    else:
        expected = Path(
            f"{tmp_path.as_posix()}/61/14/30/"
            "7c57f8fd71e4eee40b18893b9b586cba45177f15e300f4fb8b14ccc933"
        )

    assert directory == expected


def test_get_cached_archives(fixture_dir: FixtureDirGetter) -> None:
    distributions = fixture_dir("distributions")
    cache = ArtifactCache(cache_dir=Path())

    archives = cache._get_cached_archives(distributions)

    assert archives
    assert set(archives) == set(distributions.glob("*.whl")) | set(
        distributions.glob("*.tar.gz")
    )


@pytest.mark.parametrize(
    ("link", "strict", "available_packages"),
    [
        (
            "https://files.pythonhosted.org/demo-0.1.0.tar.gz",
            True,
            [
                Path("/cache/demo-0.1.0-py2.py3-none-any"),
                Path("/cache/demo-0.1.0-cp38-cp38-macosx_10_15_x86_64.whl"),
                Path("/cache/demo-0.1.0-cp37-cp37-macosx_10_15_x86_64.whl"),
            ],
        ),
        (
            "https://example.com/demo-0.1.0-cp38-cp38-macosx_10_15_x86_64.whl",
            False,
            [],
        ),
    ],
)
def test_get_not_found_cached_archive_for_link(
    mocker: MockerFixture,
    link: str,
    strict: bool,
    available_packages: list[Path],
) -> None:
    env = MockEnv(
        version_info=(3, 8, 3),
        marker_env={"interpreter_name": "cpython", "interpreter_version": "3.8.3"},
        supported_tags=[
            Tag("cp38", "cp38", "macosx_10_15_x86_64"),
            Tag("py3", "none", "any"),
        ],
    )
    cache = ArtifactCache(cache_dir=Path())

    mocker.patch.object(
        cache,
        "_get_cached_archives",
        return_value=available_packages,
    )

    archive = cache.get_cached_archive_for_link(Link(link), strict=strict, env=env)

    assert archive is None


@pytest.mark.parametrize(
    ("link", "cached", "strict"),
    [
        (
            "https://files.pythonhosted.org/demo-0.1.0.tar.gz",
            "/cache/demo-0.1.0-cp38-cp38-macosx_10_15_x86_64.whl",
            False,
        ),
        (
            "https://example.com/demo-0.1.0-cp38-cp38-macosx_10_15_x86_64.whl",
            "/cache/demo-0.1.0-cp38-cp38-macosx_10_15_x86_64.whl",
            False,
        ),
        (
            "https://files.pythonhosted.org/demo-0.1.0.tar.gz",
            "/cache/demo-0.1.0.tar.gz",
            True,
        ),
        (
            "https://example.com/demo-0.1.0-cp38-cp38-macosx_10_15_x86_64.whl",
            "/cache/demo-0.1.0-cp38-cp38-macosx_10_15_x86_64.whl",
            True,
        ),
    ],
)
def test_get_found_cached_archive_for_link(
    mocker: MockerFixture,
    link: str,
    cached: str,
    strict: bool,
) -> None:
    env = MockEnv(
        version_info=(3, 8, 3),
        marker_env={"interpreter_name": "cpython", "interpreter_version": "3.8.3"},
        supported_tags=[
            Tag("cp38", "cp38", "macosx_10_15_x86_64"),
            Tag("py3", "none", "any"),
        ],
    )
    cache = ArtifactCache(cache_dir=Path())

    mocker.patch.object(
        cache,
        "_get_cached_archives",
        return_value=[
            Path("/cache/demo-0.1.0-py2.py3-none-any"),
            Path("/cache/demo-0.1.0.tar.gz"),
            Path("/cache/demo-0.1.0-cp38-cp38-macosx_10_15_x86_64.whl"),
            Path("/cache/demo-0.1.0-cp37-cp37-macosx_10_15_x86_64.whl"),
        ],
    )

    archive = cache.get_cached_archive_for_link(Link(link), strict=strict, env=env)

    assert Path(cached) == archive


def test_get_cached_archive_for_link_no_race_condition(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    cache = ArtifactCache(cache_dir=tmp_path)
    link = Link("https://files.pythonhosted.org/demo-0.1.0.tar.gz")

    def replace_file(_: str, dest: Path) -> None:
        dest.unlink(missing_ok=True)
        # write some data (so it takes a while) to provoke possible race conditions
        dest.write_text("a" * 2**20, encoding="utf-8")

    download_mock = mocker.Mock(side_effect=replace_file)

    def get_archive(link: Link) -> Path:
        path: Path = cache.get_cached_archive_for_link(
            link, strict=True, download_func=download_mock
        )
        return path

    with concurrent.futures.ThreadPoolExecutor() as executor:
        tasks = []
        for _ in range(4):
            tasks.append(executor.submit(get_archive, link))

        concurrent.futures.wait(tasks)
        results = set()
        for task in tasks:
            try:
                results.add(task.result())
            except Exception:  # noqa: BLE001
                pytest.fail(traceback.format_exc())
        assert results == {cache.get_cache_directory_for_link(link) / link.filename}
        download_mock.assert_called_once()


def test_get_cached_archive_for_git() -> None:
    """Smoke test that checks that no assertion is raised."""
    cache = ArtifactCache(cache_dir=Path())
    archive = cache.get_cached_archive_for_git("url", "ref", "subdirectory", MockEnv())
    assert archive is None


# --------------------------------------------------------------------------- #
# Capacity governance
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, 0),
        (1024, 1024),
        ("100", 100),
        ("500 MB", 500_000_000),
        ("1KiB", 1024),
        ("1.5 KiB", 1536),
        ("2GB", 2_000_000_000),
        ("1 gib", 1024**3),
    ],
)
def test_parse_size(value: float | str, expected: int) -> None:
    assert parse_size(value) == expected


@pytest.mark.parametrize("value", ["abc", "5 XB", -1, -0.5])
def test_parse_size_invalid(value: float | str) -> None:
    with pytest.raises(ValueError):
        parse_size(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("60s", 60),
        ("90m", 5400),
        ("12h", 43200),
        ("30d", 30 * 86400),
        ("7d12h", 7 * 86400 + 12 * 3600),
        pytest.param("1", 86400, id="number-string-is-days"),
        pytest.param(1, 86400, id="number-is-days"),
    ],
)
def test_parse_duration(value: float | str, expected: int) -> None:
    assert parse_duration(value) == expected


@pytest.mark.parametrize("value", ["", "x", "30y"])
def test_parse_duration_invalid(value: str) -> None:
    with pytest.raises(ValueError):
        parse_duration(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "0 B"), (1023, "1023 B"), (1536, "1.5 KiB"), (1024**3, "1.0 GiB")],
)
def test_format_size(value: int, expected: str) -> None:
    assert format_size(value) == expected


def test_governor_disabled_is_a_noop(tmp_path: Path) -> None:
    governor = CacheGovernor("artifacts", tmp_path)
    assert not governor.enabled
    # does not raise and creates no governance data
    governor.enforce_cap()
    governor.touch(tmp_path)
    assert governor.quarantine(tmp_path) is None
    assert not (tmp_path / GOVERNANCE_DIRNAME).exists()
    stats = governor.stats()
    assert stats.max_size is None
    assert stats.policy is None
    assert stats.entries == 0


def test_governor_enforce_cap_evicts_lru(tmp_path: Path) -> None:
    cache: FileCache[Any] = FileCache(tmp_path)
    for key in ("a", "b", "c"):
        cache.put(key, "v" * 80)
    paths = {key: cache._path(key) for key in ("a", "b", "c")}
    now = time.time()
    os.utime(paths["a"], (now - 300, now - 300))
    os.utime(paths["b"], (now - 200, now - 200))
    os.utime(paths["c"], (now - 100, now - 100))

    governor = CacheGovernor("repositories", tmp_path, max_size=200)
    governor.enforce_cap()

    # Each payload is 92 bytes: only two entries fit, the oldest one is gone.
    assert not paths["a"].exists()
    assert paths["b"].exists()
    assert paths["c"].exists()
    stats = governor.stats()
    assert stats.entries == 2
    assert stats.size == 184
    assert stats.expired_entries == 0

    events = [
        json.loads(line)
        for line in (tmp_path / GOVERNANCE_DIRNAME / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    evictions = [e for e in events if e["action"] == "evict"]
    assert len(evictions) == 1
    assert evictions[0]["reason"] == str(EvictionPolicy.LRU)
    assert any(e["action"] == "enforce-cap" and e["removed"] == 1 for e in events)


def test_governor_enforce_cap_evicts_expired_before_lru(tmp_path: Path) -> None:
    cache: FileCache[Any] = FileCache(tmp_path)
    cache.put("expired", "v" * 80, minutes=-5)
    cache.put("old", "v" * 80)
    cache.put("new", "v" * 80)
    now = time.time()
    # The expired entry is used more recently than the others: nevertheless it
    # must be reclaimed first.
    os.utime(cache._path("expired"), (now - 10, now - 10))
    os.utime(cache._path("old"), (now - 500, now - 500))
    os.utime(cache._path("new"), (now - 400, now - 400))

    governor = CacheGovernor("repositories", tmp_path, max_size=200)
    governor.enforce_cap()

    assert not cache._path("expired").exists()
    assert cache._path("old").exists()
    assert cache._path("new").exists()


def test_governor_enforce_cap_respects_grace_period(tmp_path: Path) -> None:
    cache: FileCache[Any] = FileCache(tmp_path)
    cache.put("fresh", "v" * 80)

    governor = CacheGovernor("repositories", tmp_path, max_size=10)
    governor.enforce_cap()

    # the freshly written entry must not be evicted, even though the category
    # is way over its configured limit
    assert cache._path("fresh").exists()
    assert governor.stats().size == 92


def test_governor_enforce_cap_skips_locked_entries(tmp_path: Path) -> None:
    cache: FileCache[Any] = FileCache(tmp_path)
    cache.put("a", "v" * 80)
    cache.put("b", "v" * 80)
    _set_mtime(cache._path("a"), 300)
    _set_mtime(cache._path("b"), 200)

    governor = CacheGovernor("repositories", tmp_path, max_size=10)
    lock = governor.lock_for(cache._path("a"))
    lock.acquire()
    try:
        governor.enforce_cap()
        # the in-use oldest entry is kept, the next oldest is evicted
        assert cache._path("a").exists()
        assert not cache._path("b").exists()
    finally:
        lock.release()


def test_governed_put_enforces_cap_automatically(tmp_path: Path) -> None:
    governor = CacheGovernor("repositories", tmp_path, max_size=10)
    cache: FileCache[Any] = FileCache(tmp_path, governor=governor)
    cache.put("a", "v" * 80)
    _set_mtime(cache._path("a"), 300)
    cache.put("b", "v" * 80)
    _set_mtime(cache._path("b"), 200)
    cache.put("c", "v" * 80)

    assert not cache._path("a").exists()
    assert not cache._path("b").exists()
    assert cache.get("c") == "v" * 80


def test_governed_corrupt_entry_is_quarantined_and_counted(tmp_path: Path) -> None:
    governor = CacheGovernor("repositories", tmp_path, max_size=1000)
    cache: FileCache[Any] = FileCache(tmp_path, governor=governor)
    cache.put("key1", "value")
    entry = cache._path("key1")
    entry.write_text("broken", encoding="utf-8")

    assert cache.get("key1") is None
    assert not entry.exists()

    quarantine = tmp_path / GOVERNANCE_DIRNAME / "quarantine"
    quarantined = [p for p in quarantine.rglob("*") if p.is_file()]
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "broken"

    stats = governor.stats()
    assert stats.quarantined_entries == 1
    assert stats.quarantined_size == 6
    assert stats.entries == 0

    events = [
        json.loads(line)
        for line in (tmp_path / GOVERNANCE_DIRNAME / "events.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    assert events[-1]["action"] == "quarantine"
    assert events[-1]["reason"] == "corrupt"

    # a second read is a plain miss and does not raise
    assert cache.get("key1") is None


def test_corrupt_entry_without_governor_leaves_no_governance_data(
    tmp_path: Path,
) -> None:
    cache: FileCache[Any] = FileCache(tmp_path)
    cache.put("key1", "value")
    entry = cache._path("key1")
    entry.write_text("broken", encoding="utf-8")

    assert cache.get("key1") is None
    assert not entry.exists()
    assert not (tmp_path / GOVERNANCE_DIRNAME).exists()


def test_governed_get_touches_entry(tmp_path: Path) -> None:
    governor = CacheGovernor("repositories", tmp_path, max_size=1000)
    cache: FileCache[Any] = FileCache(tmp_path, governor=governor)
    cache.put("key1", "value")
    _set_mtime(cache._path("key1"), 3600)
    old_mtime = cache._path("key1").stat().st_mtime

    assert cache.get("key1") == "value"
    assert cache._path("key1").stat().st_mtime > old_mtime


def test_ungoverned_get_does_not_touch_entry(tmp_path: Path) -> None:
    cache: FileCache[Any] = FileCache(tmp_path)
    cache.put("key1", "value")
    _set_mtime(cache._path("key1"), 3600)
    old_mtime = cache._path("key1").stat().st_mtime

    assert cache.get("key1") == "value"
    assert cache._path("key1").stat().st_mtime == old_mtime


def test_governor_stats_excludes_governance_data(tmp_path: Path) -> None:
    cache: FileCache[Any] = FileCache(tmp_path)
    cache.put("a", "v" * 80)
    cache.put("b", "v" * 80)
    (tmp_path / GOVERNANCE_DIRNAME).mkdir(parents=True)
    (tmp_path / GOVERNANCE_DIRNAME / "events.jsonl").write_text("{}", encoding="utf-8")

    governor = CacheGovernor("artifacts", tmp_path)
    stats = governor.stats()
    assert stats.entries == 2
    assert stats.size == 184


def test_governor_cleanup_by_age_and_size(tmp_path: Path) -> None:
    old = tmp_path / "old.bin"
    new = tmp_path / "new.bin"
    big_old = tmp_path / "big-old.bin"
    old.write_bytes(b"o" * 100)
    new.write_bytes(b"n" * 100)
    big_old.write_bytes(b"b" * 5000)
    _set_mtime(old, 40 * 86400)
    _set_mtime(big_old, 40 * 86400)

    governor = CacheGovernor("artifacts", tmp_path)

    dry = governor.cleanup(older_than=parse_duration("30d"), dry_run=True)
    assert dry.matched_entries == 2
    assert dry.deleted_entries == 0
    assert old.exists() and big_old.exists()

    result = governor.cleanup(older_than=parse_duration("30d"))
    assert result.deleted_entries == 2
    assert not old.exists()
    assert not big_old.exists()
    assert new.exists()

    result = governor.cleanup(larger_than=parse_size("1 KiB"))
    assert result.matched_entries == 0
    big_old.write_bytes(b"b" * 5000)
    _set_mtime(big_old, 40 * 86400)
    result = governor.cleanup(larger_than=5000)
    assert result.deleted_entries == 1
    assert not big_old.exists()


def test_governor_cleanup_combines_selectors(tmp_path: Path) -> None:
    small_old = tmp_path / "small-old.bin"
    big_old = tmp_path / "big-old.bin"
    big_new = tmp_path / "big-new.bin"
    small_old.write_bytes(b"s" * 100)
    big_old.write_bytes(b"b" * 5000)
    big_new.write_bytes(b"b" * 5000)
    _set_mtime(small_old, 40 * 86400)
    _set_mtime(big_old, 40 * 86400)

    governor = CacheGovernor("artifacts", tmp_path)
    result = governor.cleanup(
        older_than=parse_duration("30d"), larger_than=parse_size("1 KiB")
    )
    assert result.matched_entries == 1
    assert not big_old.exists()
    assert small_old.exists()
    assert big_new.exists()


def test_governor_cleanup_shrink_to_evicts_lru(tmp_path: Path) -> None:
    paths = []
    for name in ("a", "b", "c"):
        path = tmp_path / f"{name}.bin"
        path.write_bytes(b"x" * 200)
        _set_mtime(path, 40 * 86400 if name == "a" else 3600)
        paths.append(path)

    governor = CacheGovernor("artifacts", tmp_path)
    result = governor.cleanup(shrink_to=300)
    assert result.deleted_entries == 2
    # the most recently used entry survives
    assert not paths[0].exists()
    assert paths[2].exists()


def test_governor_flush_keeps_governance_data(tmp_path: Path) -> None:
    governor = CacheGovernor("artifacts", tmp_path, max_size=1000)
    cache: FileCache[Any] = FileCache(tmp_path, governor=governor)
    cache.put("key1", "value")
    entry = cache._path("key1")
    entry.write_text("broken", encoding="utf-8")
    cache.get("key1")
    assert governor.stats().quarantined_entries == 1

    result = governor.flush_entries()
    assert result.matched_entries == 0
    assert result.deleted_entries == 0
    assert governor.stats().entries == 0
    assert governor.stats().quarantined_entries == 1
    assert (tmp_path / GOVERNANCE_DIRNAME / "events.jsonl").exists()


def test_file_cache_concurrent_writes_and_eviction(tmp_path: Path) -> None:
    governor = CacheGovernor("repositories", tmp_path, max_size=4096)
    cache: FileCache[Any] = FileCache(tmp_path, governor=governor)
    errors: list[str] = []

    def worker(seed: int) -> None:
        try:
            for i in range(20):
                key = f"key-{seed}-{i}"
                value = f"value-{seed}-{i}" * 20
                cache.put(key, value)
                cached = cache.get(key)
                assert cached is None or cached == value
        except Exception:  # noqa: BLE001
            errors.append(traceback.format_exc())

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(worker, range(8)))

    assert errors == []
    log = tmp_path / GOVERNANCE_DIRNAME / "events.jsonl"
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            json.loads(line)  # every recorded event must be valid JSON


def test_governor_for_category_config(config: Config) -> None:
    assert not CacheGovernor.for_category(config, CacheGovernor.ARTIFACTS).enabled

    config.merge(
        {
            "cache": {
                "categories": {
                    CacheGovernor.ARTIFACTS: {"max-size": "1 KiB"},
                }
            }
        }
    )
    governor = CacheGovernor.for_category(config, CacheGovernor.ARTIFACTS)
    assert governor.enabled
    assert governor.max_size == 1024
    assert governor.policy is EvictionPolicy.LRU
    assert governor is CacheGovernor.for_category(config, CacheGovernor.ARTIFACTS)
    assert governor.root == config.artifacts_cache_directory


def test_governor_for_category_invalid_config(config: Config) -> None:
    config.merge({"cache": {"categories": {"repositories": {"max-size": "nope"}}}})
    with pytest.raises(ValueError, match="Invalid size"):
        CacheGovernor.for_category(config, CacheGovernor.REPOSITORIES)

    config.merge({"cache": {"categories": {"artifacts": {"policy": "fifo"}}}})
    with pytest.raises(ValueError, match="Unknown eviction policy"):
        CacheGovernor.for_category(config, CacheGovernor.ARTIFACTS)

    with pytest.raises(ValueError, match="Unknown cache category"):
        CacheGovernor.for_category(config, "unknown")


def test_artifact_cache_governor_touches_archive_on_hit(tmp_path: Path) -> None:
    link = Link("https://files.pythonhosted.org/demo-0.1.0.tar.gz")

    governed = ArtifactCache(
        cache_dir=tmp_path / "gov",
        governor=CacheGovernor("artifacts", tmp_path / "gov", max_size=10_000),
    )
    directory = governed.get_cache_directory_for_link(link)
    directory.mkdir(parents=True)
    archive = directory / link.filename
    archive.write_bytes(b"demo archive")
    _set_mtime(archive, 3600)
    old_mtime = archive.stat().st_mtime

    assert governed.get_cached_archive_for_link(link, strict=True) == archive
    assert archive.stat().st_mtime > old_mtime

    ungoverned_dir = tmp_path / "plain"
    ungoverned = ArtifactCache(cache_dir=ungoverned_dir)
    directory2 = ungoverned.get_cache_directory_for_link(link)
    directory2.mkdir(parents=True)
    archive2 = directory2 / link.filename
    archive2.write_bytes(b"demo archive")
    _set_mtime(archive2, 3600)
    old_mtime2 = archive2.stat().st_mtime

    assert ungoverned.get_cached_archive_for_link(link, strict=True) == archive2
    assert archive2.stat().st_mtime == old_mtime2


def test_artifact_cache_governor_enforces_after_download(tmp_path: Path) -> None:
    governor = CacheGovernor("artifacts", tmp_path, max_size=100)
    cache = ArtifactCache(cache_dir=tmp_path, governor=governor)

    old_link = Link("https://example.org/old-1.0.tar.gz")
    old_dir = cache.get_cache_directory_for_link(old_link)
    old_dir.mkdir(parents=True)
    old_archive = old_dir / old_link.filename
    old_archive.write_bytes(b"o" * 500)
    _set_mtime(old_archive, 3600)

    new_link = Link("https://example.org/new-2.0.tar.gz")

    def download(_url: str, dest: Path) -> None:
        dest.write_bytes(b"n" * 2000)

    result = cache.get_cached_archive_for_link(
        new_link, strict=True, download_func=download
    )

    assert result == cache.get_cache_directory_for_link(new_link) / new_link.filename
    assert result.exists()
    assert not old_archive.exists()


def test_get_cached_archive_for_link_no_race_condition_governed(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    governor = CacheGovernor("artifacts", tmp_path, max_size=1)
    cache = ArtifactCache(cache_dir=tmp_path, governor=governor)
    link = Link("https://files.pythonhosted.org/demo-0.1.0.tar.gz")

    def replace_file(_: str, dest: Path) -> None:
        dest.unlink(missing_ok=True)
        dest.write_text("a" * 2**20, encoding="utf-8")

    download_mock = mocker.Mock(side_effect=replace_file)

    def get_archive(link: Link) -> Path:
        result: Path | None = cache.get_cached_archive_for_link(
            link, strict=True, download_func=download_mock
        )
        assert result is not None
        return result

    with concurrent.futures.ThreadPoolExecutor() as executor:
        tasks = [executor.submit(get_archive, link) for _ in range(4)]
        results = set()
        for task in tasks:
            try:
                results.add(task.result())
            except Exception:  # noqa: BLE001
                pytest.fail(traceback.format_exc())

    assert results == {cache.get_cache_directory_for_link(link) / link.filename}
    download_mock.assert_called_once()
