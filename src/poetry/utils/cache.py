from __future__ import annotations

import dataclasses
import enum
import hashlib
import itertools
import json
import logging
import os
import re
import shutil
import stat
import threading
import time
import weakref

from collections import defaultdict
from contextlib import contextmanager
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import Generic
from typing import TypeVar
from typing import overload

from requests.utils import atomic_open

from poetry.utils._compat import decode
from poetry.utils._compat import encode
from poetry.utils.helpers import get_highest_priority_hash_type
from poetry.utils.wheel import InvalidWheelNameError
from poetry.utils.wheel import Wheel


if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterator

    from poetry.core.packages.utils.link import Link

    from poetry.config.config import Config
    from poetry.utils.env import Env


# Used by FileCache for items that do not expire.
MAX_DATE = 9999999999
T = TypeVar("T")

logger = logging.getLogger(__name__)

# Directory (relative to a cache category root) holding governance data:
# the eviction/quarantine event log, quarantined entries and the advisory
# cross-process lock. It is never counted as cache data or evicted.
GOVERNANCE_DIRNAME = ".poetry-cache"
_EVENTS_LOG_NAME = "events.jsonl"
_EVENTS_LOG_ROTATED_NAME = "events.log.1"
_QUARANTINE_DIRNAME = "quarantine"
_LOCK_DIRNAME = "lock"
# Entries written within this many seconds are never evicted, so that an entry
# which is currently being produced (download, build, write) cannot be torn by
# a concurrent governance action.
_EVICTION_GRACE_SECONDS = 60.0
_EVENTS_LOG_MAX_BYTES = 1024 * 1024
_ENFORCE_MIN_INTERVAL_SECONDS = 30.0
_CLEANUP_LOCK_WAIT_SECONDS = 10.0
_LOCK_STALE_SECONDS = 300.0


_SIZE_UNITS: dict[str, int] = {
    "b": 1,
    "k": 1000,
    "kb": 1000,
    "ki": 1024,
    "kib": 1024,
    "m": 1000**2,
    "mb": 1000**2,
    "mi": 1024**2,
    "mib": 1024**2,
    "g": 1000**3,
    "gb": 1000**3,
    "gi": 1024**3,
    "gib": 1024**3,
    "t": 1000**4,
    "tb": 1000**4,
    "ti": 1024**4,
    "tib": 1024**4,
}

_DURATION_UNITS: dict[str, int] = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 7 * 86400,
}


def parse_size(value: float | str) -> int:
    """
    Parse a byte size given as a number (bytes) or a string such as
    ``524288000``, ``"500 MB"`` or ``"1 GiB"``.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError(f"Invalid size: {value!r}")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError(f"Invalid size: {value!r}")
        return int(value)

    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*", value)
    if not match:
        raise ValueError(
            f"Invalid size {value!r}. Expected a number of bytes (e.g. 524288000)"
            " or a string with a unit (e.g. '500 MB' or '1 GiB')."
        )
    amount = float(match.group(1))
    # a value without a unit is a number of bytes
    unit = match.group(2).lower() or "b"
    if unit not in _SIZE_UNITS:
        raise ValueError(
            f"Unknown size unit {unit!r} in {value!r}. Valid units are "
            "B, KB/MB/GB/TB (decimal) and KiB/MiB/GiB/TiB (binary)."
        )
    return int(amount * _SIZE_UNITS[unit])


def parse_duration(value: float | str) -> int:
    """
    Parse a duration such as ``"30d"``, ``"12h"`` or ``"7d12h"`` into seconds.
    A plain number (without unit) is interpreted as days.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise TypeError(f"Invalid duration: {value!r}")
    if isinstance(value, (int, float)):
        if value < 0:
            raise ValueError(f"Invalid duration: {value!r}")
        return int(value * 86400)

    text = value.strip().lower()
    if not text:
        raise ValueError("Invalid duration: empty value")
    if re.fullmatch(r"\d+(?:\.\d+)?", text):
        return int(float(text) * 86400)

    total = 0.0
    position = 0
    for match_ in re.finditer(r"(\d+(?:\.\d+)?)([smhdw])", text):
        if match_.start() != position:
            raise ValueError(
                f"Invalid duration {value!r}. Use a number with a unit, e.g."
                " '30d', '12h' or '7d12h' (units: s, m, h, d, w)."
            )
        total += float(match_.group(1)) * _DURATION_UNITS[match_.group(2)]
        position = match_.end()
    if position != len(text):
        raise ValueError(
            f"Invalid duration {value!r}. Use a number with a unit, e.g."
            " '30d', '12h' or '7d12h' (units: s, m, h, d, w)."
        )
    return int(total)


def format_size(num_bytes: int) -> str:
    """Format a number of bytes as a human readable size (binary units)."""
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if abs(size) < 1024.0 or unit == "PiB":
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PiB"


class EvictionPolicy(enum.Enum):
    LRU = "lru"

    def __str__(self) -> str:
        return self.value


@dataclasses.dataclass(frozen=True)
class CacheStats:
    """
    Usage statistics of one cache category.

    :param category: category name (e.g. ``artifacts``)
    :param path: root directory of the category
    :param entries: number of cache entry files
    :param size: total size of the entries in bytes
    :param expired_entries: number of entries whose expiry timestamp passed
    :param max_size: configured maximum size in bytes (``None`` if unlimited)
    :param policy: configured eviction policy (``None`` if unlimited)
    :param quarantined_entries: number of quarantined entries
    :param quarantined_size: size of quarantined entries in bytes
    """

    category: str
    path: Path
    entries: int
    size: int
    expired_entries: int
    max_size: int | None
    policy: EvictionPolicy | None
    quarantined_entries: int
    quarantined_size: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "path": str(self.path),
            "entries": self.entries,
            "size": self.size,
            "expired_entries": self.expired_entries,
            "max_size": self.max_size,
            "policy": str(self.policy) if self.policy is not None else None,
            "quarantined": {
                "entries": self.quarantined_entries,
                "size": self.quarantined_size,
            },
        }


@dataclasses.dataclass(frozen=True)
class CleanupResult:
    """
    Result of a selective cleanup.

    :param matched_entries: number of entries matching the cleanup criteria
    :param matched_bytes: size of the matching entries in bytes
    :param deleted_entries: number of entries actually deleted (0 for dry runs)
    :param freed_bytes: number of bytes actually freed (0 for dry runs)
    """

    matched_entries: int
    matched_bytes: int
    deleted_entries: int
    freed_bytes: int


def _pid_alive(pid: int) -> bool | None:
    """
    Return ``True``/``False`` if the liveness of ``pid`` can be determined,
    ``None`` otherwise.
    """
    try:
        if os.name == "nt":
            import ctypes

            # PROCESS_QUERY_LIMITED_INFORMATION
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                # ERROR_INVALID_PARAMETER means there is no such process
                return False if ctypes.get_last_error() == 87 else None
            kernel32.CloseHandle(handle)
            return True

        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    else:
        return True


class _CrossProcessLock:
    """
    Best-effort advisory lock backed by an atomically created directory.

    Governance never blocks regular cache reads or writes: callers acquire the
    lock non-blocking and skip the governance action if another process holds
    it. Explicit cleanup actions may wait for a short, bounded time.
    """

    def __init__(self, path: Path, *, stale_after: float = _LOCK_STALE_SECONDS) -> None:
        self._path = path
        self._stale_after = stale_after

    @contextmanager
    def acquired(self, *, wait: float = 0.0) -> Iterator[bool]:
        held = self._acquire(wait=wait)
        try:
            yield held
        finally:
            if held:
                self._release()

    def _acquire(self, *, wait: float) -> bool:
        deadline = time.monotonic() + max(wait, 0.0)
        while True:
            try:
                self._path.mkdir(parents=True, exist_ok=False)
            except FileExistsError:
                self._reap_if_stale()
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.05)
            else:
                self._write_holder()
                return True

    def _release(self) -> None:
        shutil.rmtree(self._path, ignore_errors=True)

    def _write_holder(self) -> None:
        # the lock directory itself is the source of truth; the holder info is
        # best effort for stale lock detection
        with suppress(OSError):
            (self._path / "holder.json").write_text(
                json.dumps({"pid": os.getpid(), "created": time.time()}),
                encoding="utf-8",
            )

    def _reap_if_stale(self) -> None:
        holder = self._path / "holder.json"
        pid: int | None = None
        try:
            age = time.time() - holder.stat().st_mtime
            pid = int(json.loads(holder.read_text(encoding="utf-8"))["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            try:
                age = time.time() - self._path.stat().st_mtime
            except OSError:
                return
        if age < self._stale_after:
            return
        if pid is not None and _pid_alive(pid) is True:
            return
        shutil.rmtree(self._path, ignore_errors=True)


def _expiration(minutes: int) -> int:
    """
    Calculates the time in seconds since epoch that occurs 'minutes' from now.

    :param minutes: The number of minutes to count forward
    """
    return round(time.time()) + minutes * 60


_HASHES = {
    "md5": (hashlib.md5, 2),
    "sha1": (hashlib.sha1, 4),
    "sha256": (hashlib.sha256, 8),
}


@dataclasses.dataclass(frozen=True)
class CacheItem(Generic[T]):
    """
    Stores data and metadata for cache items.
    """

    data: T
    expires: int | None = None

    @property
    def expired(self) -> bool:
        """
        Return true if the cache item has exceeded its expiration period.
        """
        return self.expires is not None and time.time() >= self.expires


@dataclasses.dataclass(frozen=True)
class FileCache(Generic[T]):
    """
    Cachy-compatible minimal file cache. Stores subsequent data in a JSON format.

    :param path: The path that the cache starts at.
    :param hash_type: The hash to use for encoding keys/building directories.
    :param governor: Optional :class:`CacheGovernor` of the cache category this
        cache belongs to. When no capacity limit is configured the governor is
        a no-op and caching behaviour is unchanged.
    """

    path: Path
    hash_type: str = "sha256"
    governor: CacheGovernor | None = None

    def __post_init__(self) -> None:
        if self.hash_type not in _HASHES:
            raise ValueError(
                f"FileCache.hash_type is unknown value: '{self.hash_type}'."
            )

    def get(self, key: str) -> T | None:
        return self._get_payload(key)

    def has(self, key: str) -> bool:
        """
        Determine if a file exists and has not expired in the cache.
        :param key: The cache key
        :returns: True if the key exists in the cache
        """
        return self.get(key) is not None

    def put(self, key: str, value: Any, minutes: int | None = None) -> None:
        """
        Store an item in the cache.

        :param key: The cache key
        :param value: The cache value
        :param minutes: The lifetime in minutes of the cached value
        """
        payload: CacheItem[Any] = CacheItem(
            value, expires=_expiration(minutes) if minutes is not None else None
        )
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temporary file and replace the entry atomically, so a
        # concurrent reader or evictor can never observe a half-written entry.
        with atomic_open(os.fspath(path)) as f:
            f.write(self._serialize(payload))
        if self.governor is not None:
            try:
                written_bytes = path.stat().st_size
            except OSError:
                written_bytes = 0
            self.governor.enforce_cap(written_bytes=written_bytes)

    def forget(self, key: str) -> None:
        """
        Remove an item from the cache.

        :param key: The cache key
        """
        self._path(key).unlink(missing_ok=True)

    def flush(self) -> None:
        """
        Clear the cache.
        """
        shutil.rmtree(self.path)

    def remember(
        self, key: str, callback: T | Callable[[], T], minutes: int | None = None
    ) -> T:
        """
        Get an item from the cache, or use a default from callback.

        :param key: The cache key
        :param callback: Callback function providing default value
        :param minutes: The lifetime in minutes of the cached value
        """
        value = self.get(key)
        if value is None:
            value = callback() if callable(callback) else callback
            self.put(key, value, minutes)
        return value

    def _get_payload(self, key: str) -> T | None:
        path = self._path(key)

        if not path.exists():
            return None

        try:
            with path.open("rb") as f:
                file_content = f.read()
        except FileNotFoundError:
            # the entry was removed by a concurrent forget/eviction
            return None

        try:
            payload = self._deserialize(file_content)
        except (json.JSONDecodeError, ValueError):
            if self.governor is not None and self.governor.quarantine(path):
                # The corrupt entry was quarantined (and counted); the
                # governor logged where it was moved to.
                return None
            self.forget(key)
            logger.warning("Corrupt cache file was detected and cleaned up.")
            return None

        if payload.expired:
            self.forget(key)
            return None

        if self.governor is not None:
            # Record the usage so that LRU eviction can order entries by their
            # last use rather than only by the time they were written.
            self.governor.touch(path)
        return payload.data

    def _path(self, key: str) -> Path:
        hash_type, parts_count = _HASHES[self.hash_type]
        h = hash_type(encode(key)).hexdigest()
        parts = [h[i : i + 2] for i in range(0, len(h), 2)][:parts_count]
        return Path(self.path, *parts, h)

    def _serialize(self, payload: CacheItem[T]) -> bytes:
        expires = payload.expires or MAX_DATE
        data = json.dumps(payload.data)
        return encode(f"{expires:010d}{data}")

    def _deserialize(self, data_raw: bytes) -> CacheItem[T]:
        data_str = decode(data_raw)
        data = json.loads(data_str[10:])
        expires = int(data_str[:10])
        return CacheItem(data, expires)


class ArtifactCache:
    def __init__(
        self, *, cache_dir: Path, governor: CacheGovernor | None = None
    ) -> None:
        self._cache_dir = cache_dir
        self._governor = governor
        self._archive_locks: defaultdict[Path, threading.Lock] = defaultdict(
            threading.Lock
        )

    def _archive_lock(self, archive: Path) -> threading.Lock:
        # When the category is governed, share the governor's per-entry lock
        # registry so that eviction can never remove an archive that is being
        # downloaded or used in-process.
        if self._governor is not None and self._governor.enabled:
            return self._governor.lock_for(archive)
        return self._archive_locks[archive]

    def _mark_used(self, archive: Path) -> Path:
        if self._governor is not None:
            self._governor.touch(archive)
        return archive

    def get_cache_directory_for_link(self, link: Link) -> Path:
        key_parts = {"url": link.url_without_fragment}

        if hash_name := get_highest_priority_hash_type(link.hashes, link.filename):
            key_parts[hash_name] = link.hashes[hash_name]

        if link.subdirectory_fragment:
            key_parts["subdirectory"] = link.subdirectory_fragment

        return self._get_directory_from_hash(key_parts)

    def _get_directory_from_hash(self, key_parts: object) -> Path:
        key = hashlib.sha256(
            json.dumps(
                key_parts, sort_keys=True, separators=(",", ":"), ensure_ascii=True
            ).encode("ascii")
        ).hexdigest()

        split_key = [key[:2], key[2:4], key[4:6], key[6:]]
        return self._cache_dir.joinpath(*split_key)

    def get_cache_directory_for_git(
        self, url: str, ref: str, subdirectory: str | None
    ) -> Path:
        key_parts = {"url": url, "ref": ref}
        if subdirectory:
            key_parts["subdirectory"] = subdirectory

        return self._get_directory_from_hash(key_parts)

    @overload
    def get_cached_archive_for_link(
        self,
        link: Link,
        *,
        strict: bool,
        env: Env | None = ...,
        download_func: Callable[[str, Path], None],
    ) -> Path: ...

    @overload
    def get_cached_archive_for_link(
        self,
        link: Link,
        *,
        strict: bool,
        env: Env | None = ...,
        download_func: None = ...,
    ) -> Path | None: ...

    def get_cached_archive_for_link(
        self,
        link: Link,
        *,
        strict: bool,
        env: Env | None = None,
        download_func: Callable[[str, Path], None] | None = None,
    ) -> Path | None:
        cache_dir = self.get_cache_directory_for_link(link)

        cached_archive = self._get_cached_archive(
            cache_dir, strict=strict, filename=link.filename, env=env
        )
        downloaded = False
        if cached_archive is None and strict and download_func is not None:
            # TODO: remove check as soon as this is handled in poetry-core
            if "/" in link.filename or "\\" in link.filename:
                raise ValueError(f"Invalid filename: '{link.filename}'")
            cached_archive = cache_dir / link.filename
            with self._archive_lock(cached_archive):
                # Check again if the archive exists (under the lock) to avoid
                # duplicate downloads because it may have already been downloaded
                # by another thread in the meantime
                if not cached_archive.exists():
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    try:
                        download_func(link.url, cached_archive)
                    except BaseException:
                        cached_archive.unlink(missing_ok=True)
                        raise
                    downloaded = True

        if downloaded:
            assert cached_archive is not None
            if self._governor is not None:
                try:
                    written_bytes = cached_archive.stat().st_size
                except OSError:
                    written_bytes = 0
                self._governor.enforce_cap(written_bytes=written_bytes)

        return cached_archive

    def get_cached_archive_for_git(
        self, url: str, reference: str, subdirectory: str | None, env: Env
    ) -> Path | None:
        cache_dir = self.get_cache_directory_for_git(url, reference, subdirectory)

        return self._get_cached_archive(cache_dir, strict=False, env=env)

    def _get_cached_archive(
        self,
        cache_dir: Path,
        *,
        strict: bool,
        filename: str | None = None,
        env: Env | None = None,
    ) -> Path | None:
        # implication "not strict -> env must not be None"
        assert strict or env is not None
        # implication "strict -> filename must not be None"
        assert not strict or filename is not None

        archives = self._get_cached_archives(cache_dir)
        if not archives:
            return None

        candidates: list[tuple[float | None, Path]] = []

        for archive in archives:
            if strict:
                # in strict mode return the original cached archive instead of the
                # prioritized archive type.
                if filename == archive.name:
                    return self._mark_used(archive)
                continue

            assert env is not None

            if archive.suffix != ".whl":
                candidates.append((float("inf"), archive))
                continue

            try:
                wheel = Wheel(archive.name)
            except InvalidWheelNameError:
                continue

            if not wheel.is_supported_by_environment(env):
                continue

            candidates.append(
                (wheel.get_minimum_supported_index(env.supported_tags), archive),
            )

        if not candidates:
            return None

        return self._mark_used(min(candidates)[1])

    def _get_cached_archives(self, cache_dir: Path) -> list[Path]:
        archive_types = ["whl", "tar.gz", "tar.bz2", "bz2", "zip"]
        paths: list[Path] = []
        for archive_type in archive_types:
            paths += cache_dir.glob(f"*.{archive_type}")

        return paths


class CacheGovernor:
    """
    Capacity governance for one cache category directory.

    A category is a directory tree containing cache entry files, e.g. the
    ``artifacts`` directory (downloaded archives and built wheels) or the
    ``cache/repositories`` directory (repository metadata and HTTP caches).

    When a ``max_size`` is configured for a category:

    * writes that push the category beyond the limit evict expired entries
      first and then the least recently used entries;
    * corrupt entries are moved to a quarantine area (and counted) instead of
      being deleted silently;
    * every eviction, quarantine and cleanup action is recorded in an event
      log (``<root>/.poetry-cache/events.jsonl``).

    Without a configured limit every governance operation is a no-op, so the
    existing caching semantics are preserved verbatim. Statistics and
    selective cleanup are available regardless of whether a limit is set.
    """

    REPOSITORIES = "repositories"
    ARTIFACTS = "artifacts"
    CATEGORIES = (REPOSITORIES, ARTIFACTS)

    # One governor instance per (category, resolved root) per process, so
    # that entry locks and enforcement state are shared across all caches of
    # the same category (e.g. all per-repository FileCache instances).
    _registry: weakref.WeakValueDictionary[str, CacheGovernor] = (
        weakref.WeakValueDictionary()
    )
    _registry_lock = threading.Lock()

    def __init__(
        self,
        category: str,
        root: Path,
        *,
        max_size: int | None = None,
        policy: EvictionPolicy = EvictionPolicy.LRU,
    ) -> None:
        self.category = category
        self.root = Path(root)
        self.max_size = max_size
        self.policy = policy
        self._root_resolved = self.root.expanduser().resolve(strict=False)
        self._lock = threading.RLock()
        self._entry_locks: dict[Path, threading.Lock] = {}
        self._event_lock = threading.Lock()
        self._quarantine_counter = itertools.count(1)
        self._cross_process_lock = _CrossProcessLock(
            self.root / GOVERNANCE_DIRNAME / _LOCK_DIRNAME
        )
        self._last_enforce_at = 0.0
        self._last_total_size = 0

    @classmethod
    def for_category(cls, config: Config, category: str) -> CacheGovernor:
        """
        Return the shared governor for ``category`` based on the given
        configuration. A governor is always returned; it is enabled (active)
        only when a ``max-size`` is declared for the category.
        """
        if category not in cls.CATEGORIES:
            raise ValueError(
                f"Unknown cache category {category!r}. Valid categories: "
                f"{', '.join(cls.CATEGORIES)}."
            )
        root = cls._root_for(config, category)

        settings = config.get(f"cache.categories.{category}")
        if not isinstance(settings, dict):
            settings = {}

        max_size: int | None = None
        if settings.get("max-size") is not None:
            max_size = parse_size(settings["max-size"])

        policy_value = settings.get("policy", EvictionPolicy.LRU.value)
        try:
            policy = EvictionPolicy(str(policy_value))
        except ValueError:
            raise ValueError(
                f"Unknown eviction policy {policy_value!r} for cache category"
                f" {category!r}. Valid policies: "
                f"{', '.join(p.value for p in EvictionPolicy)}."
            ) from None

        key = f"{category}:{Path(root).expanduser().resolve(strict=False)}"
        with cls._registry_lock:
            governor = cls._registry.get(key)
            if (
                governor is None
                or governor.max_size != max_size
                or governor.policy != policy
            ):
                governor = cls(category, Path(root), max_size=max_size, policy=policy)
                cls._registry[key] = governor
            return governor

    @staticmethod
    def _root_for(config: Config, category: str) -> Path:
        if category == CacheGovernor.ARTIFACTS:
            return config.artifacts_cache_directory
        return config.repository_cache_directory

    @property
    def enabled(self) -> bool:
        """Whether capacity enforcement/quarantine is active for the category."""
        return self.max_size is not None

    @property
    def _governance_dir(self) -> Path:
        return self.root / GOVERNANCE_DIRNAME

    @property
    def _events_log(self) -> Path:
        return self._governance_dir / _EVENTS_LOG_NAME

    @property
    def _quarantine_dir(self) -> Path:
        return self._governance_dir / _QUARANTINE_DIRNAME

    def lock_for(self, path: Path) -> threading.Lock:
        """
        Return the in-process lock guarding an entry file.

        Producers (e.g. artifact downloads) hold this lock while an entry is
        being created; eviction probes the same lock and skips entries that
        are currently in use.
        """
        with self._lock:
            lock = self._entry_locks.get(path)
            if lock is None:
                lock = self._entry_locks[path] = threading.Lock()
            return lock

    def touch(self, path: Path) -> None:
        """Record that an entry was used (LRU bookkeeping). Best effort."""
        if not self.enabled:
            return
        try:
            os.utime(path, None)
        except OSError:
            logger.debug(
                "Unable to update last-used time of cache entry %s",
                path,
                exc_info=True,
            )

    def quarantine(self, path: Path, *, reason: str = "corrupt") -> Path | None:
        """
        Move a damaged entry into the quarantine area instead of deleting it.

        Returns the quarantine location or ``None`` when governance is
        disabled, the entry no longer exists or it could not be moved.
        """
        if not self.enabled:
            return None
        with self._lock:
            try:
                if not path.is_file():
                    return None
                size = path.stat().st_size
            except OSError:
                return None
            stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime())
            name = f"{stamp}-{os.getpid()}-{next(self._quarantine_counter):04d}"
            destination_dir = self._quarantine_dir / name
            try:
                destination_dir.mkdir(parents=True, exist_ok=False)
                destination = destination_dir / path.name
                os.replace(path, destination)
            except OSError:
                logger.debug(
                    "Unable to quarantine corrupt cache entry %s",
                    path,
                    exc_info=True,
                )
                # remove a half-created quarantine bucket
                shutil.rmtree(destination_dir, ignore_errors=True)
                return None
            self._prune_empty_parent_dirs(path)

        self._record_event(
            action="quarantine",
            reason=reason,
            entry=self._relative(path),
            destination=self._relative(destination),
            bytes=size,
        )
        logger.warning(
            "Corrupt cache entry was quarantined: %s -> %s", path, destination
        )
        return destination

    def stats(self) -> CacheStats:
        """Return current usage statistics of the category."""
        now = time.time()
        entries = size = expired = 0
        for _path, st, expires_at in self._scan():
            entries += 1
            size += st.st_size
            if expires_at is not None and now >= expires_at:
                expired += 1
        quarantined_entries, quarantined_size = self._quarantine_stats()
        return CacheStats(
            category=self.category,
            path=self.root,
            entries=entries,
            size=size,
            expired_entries=expired,
            max_size=self.max_size,
            policy=self.policy if self.enabled else None,
            quarantined_entries=quarantined_entries,
            quarantined_size=quarantined_size,
        )

    def enforce_cap(self, written_bytes: int = 0) -> None:
        """
        Evict entries until the category fits its configured maximum.

        Safe to call after every write: enforcement is debounced in-process
        (but always runs when the just-written bytes would exceed the limit)
        and is skipped without blocking when another process governs the
        category concurrently.
        """
        max_size = self.max_size
        if max_size is None:
            return
        now = time.time()
        with self._lock:
            if (
                now - self._last_enforce_at < _ENFORCE_MIN_INTERVAL_SECONDS
                and self._last_total_size + written_bytes <= max_size
            ):
                # Keep the estimated total accounting up to date with the
                # writes we did not scan, so the next write can still notice
                # when the category grows beyond its limit.
                self._last_total_size += written_bytes
                return
            self._last_enforce_at = now
            with self._cross_process_lock.acquired() as acquired:
                if not acquired:
                    self._last_total_size += written_bytes
                    logger.debug(
                        "Cache governance for category '%s' is active in another"
                        " process; skipping enforcement this time",
                        self.category,
                    )
                    return
                self._enforce_cap_locked(now)

    def cleanup(
        self,
        *,
        older_than: int | None = None,
        larger_than: int | None = None,
        shrink_to: int | None = None,
        dry_run: bool = False,
    ) -> CleanupResult:
        """
        Remove entries selectively.

        :param older_than: remove entries not used since this many seconds
        :param larger_than: remove entries of at least this many bytes
        :param shrink_to: additionally evict least recently used entries until
            the category uses at most this many bytes
        :param dry_run: only report what would be removed
        """
        now = time.time()
        with (
            self._lock,
            self._cross_process_lock.acquired(
                wait=_CLEANUP_LOCK_WAIT_SECONDS
            ) as acquired,
        ):
            if not acquired:
                raise RuntimeError(
                    f"Cache category '{self.category}' is currently being"
                    " governed by another process; please try again later."
                )
            return self._cleanup_locked(
                now=now,
                older_than=older_than,
                larger_than=larger_than,
                shrink_to=shrink_to,
                dry_run=dry_run,
            )

    def flush_entries(self) -> CleanupResult:
        """
        Remove all cache entries of the category. Governance data (event log
        and quarantine area) is retained.
        """
        with (
            self._lock,
            self._cross_process_lock.acquired(
                wait=_CLEANUP_LOCK_WAIT_SECONDS
            ) as acquired,
        ):
            if not acquired:
                raise RuntimeError(
                    f"Cache category '{self.category}' is currently being"
                    " governed by another process; please try again later."
                )
            current = self.stats()
            if not self.root.exists():
                return CleanupResult(0, 0, 0, 0)
            for child in list(self.root.iterdir()):
                if child.name == GOVERNANCE_DIRNAME:
                    continue
                if child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    try:
                        child.unlink()
                    except OSError:
                        # held open (typically on Windows); leave it for
                        # the next cleanup and report it as in use
                        logger.debug(
                            "Unable to remove cache entry during flush: %s",
                            child,
                            exc_info=True,
                        )
            remaining = self.stats()
            deleted = current.entries - remaining.entries
            freed = current.size - remaining.size
            self._record_event(
                action="flush",
                deleted=deleted,
                bytes_freed=freed,
            )
            return CleanupResult(current.entries, current.size, deleted, freed)

    # -- internals ----------------------------------------------------------

    def _enforce_cap_locked(self, now: float) -> None:
        assert self.max_size is not None
        scanned = self._scan()
        total_before = sum(st.st_size for _path, st, _expires_at in scanned)
        self._last_total_size = total_before
        if total_before <= self.max_size:
            return

        expired = [
            (path, st, True)
            for path, st, expires_at in scanned
            if expires_at is not None and now >= expires_at
        ]
        expired_paths = {path for path, _st, _is_expired in expired}
        expired.sort(key=lambda item: item[1].st_mtime)
        others = sorted(
            (
                (path, st, False)
                for path, st, _expires_at in scanned
                if path not in expired_paths
            ),
            key=lambda item: (item[1].st_mtime, str(item[0])),
        )
        victims = expired + others

        current = total_before
        freed = 0
        removed = 0
        blocked = 0
        for path, st, is_expired in victims:
            if current <= self.max_size:
                break
            if not is_expired and st.st_mtime > now - _EVICTION_GRACE_SECONDS:
                # written very recently: likely being produced right now
                blocked += 1
                continue
            reason = "expired" if is_expired else str(self.policy)
            freed_bytes = self._evict(path, reason=reason)
            if freed_bytes is None:
                blocked += 1
                continue
            current -= freed_bytes
            freed += freed_bytes
            removed += 1

        self._last_total_size = current
        self._record_event(
            action="enforce-cap",
            removed=removed,
            bytes_freed=freed,
            bytes_before=total_before,
            bytes_after=current,
            max_size=self.max_size,
        )
        if current > self.max_size:
            logger.warning(
                "Cache category '%s' is %s over its configured maximum of %s;"
                " %d %s not evicted because %s in use or written within the"
                " last %d seconds",
                self.category,
                format_size(current - self.max_size),
                format_size(self.max_size),
                blocked,
                "entry is" if blocked == 1 else "entries are",
                "it is" if blocked == 1 else "they are",
                int(_EVICTION_GRACE_SECONDS),
            )

    def _cleanup_locked(
        self,
        *,
        now: float,
        older_than: int | None,
        larger_than: int | None,
        shrink_to: int | None,
        dry_run: bool,
    ) -> CleanupResult:
        scanned = self._scan()
        selected: list[tuple[Path, int, str]] = []
        for path, st, _expires_at in scanned:
            conditions: list[bool] = []
            if older_than is not None:
                conditions.append(st.st_mtime <= now - older_than)
            if larger_than is not None:
                conditions.append(st.st_size >= larger_than)
            if conditions and all(conditions):
                selected.append((path, st.st_size, "selectors"))

        selected_paths = {path for path, _size, _reason in selected}
        if shrink_to is not None:
            projected = sum(st.st_size for _p, st, _e in scanned) - sum(
                size for _path, size, _reason in selected
            )
            if projected > shrink_to:
                for path, st, _expires_at in sorted(
                    scanned, key=lambda item: (item[1].st_mtime, str(item[0]))
                ):
                    if projected <= shrink_to:
                        break
                    if path in selected_paths:
                        continue
                    if st.st_mtime > now - _EVICTION_GRACE_SECONDS:
                        continue
                    selected.append((path, st.st_size, "shrink-to"))
                    selected_paths.add(path)
                    projected -= st.st_size

        matched_entries = len(selected)
        matched_bytes = sum(size for _path, size, _reason in selected)
        if dry_run:
            return CleanupResult(matched_entries, matched_bytes, 0, 0)

        deleted_entries = 0
        freed_bytes = 0
        blocked = 0
        for path, size, kind in selected:
            if self._evict(path, reason=f"cleanup:{kind}") is None:
                blocked += 1
                continue
            deleted_entries += 1
            freed_bytes += size

        self._record_event(
            action="cleanup",
            matched=matched_entries,
            deleted=deleted_entries,
            bytes_freed=freed_bytes,
            older_than=older_than,
            larger_than=larger_than,
            shrink_to=shrink_to,
        )
        if blocked:
            logger.warning(
                "%d %s skipped during cleanup of cache category '%s' because"
                " %s in use or written within the last %d seconds",
                blocked,
                "entry" if blocked == 1 else "entries",
                self.category,
                "it is" if blocked == 1 else "they are",
                int(_EVICTION_GRACE_SECONDS),
            )
        return CleanupResult(
            matched_entries, matched_bytes, deleted_entries, freed_bytes
        )

    def _scan(self) -> list[tuple[Path, os.stat_result, int | None]]:
        entries: list[tuple[Path, os.stat_result, int | None]] = []
        if not self.root.exists():
            return entries
        for path in self.root.rglob("*"):
            try:
                relative = path.relative_to(self.root)
            except ValueError:
                continue
            if GOVERNANCE_DIRNAME in relative.parts:
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            entries.append((path, st, self._expiry(path)))
        return entries

    @staticmethod
    def _expiry(path: Path) -> int | None:
        """Return the FileCache expiry timestamp encoded in a file, if any."""
        try:
            with open(path, "rb") as f:
                prefix = f.read(10)
        except OSError:
            return None
        if len(prefix) != 10 or not prefix.isdigit():
            return None
        return int(prefix)

    def _quarantine_stats(self) -> tuple[int, int]:
        if not self._quarantine_dir.exists():
            return 0, 0
        count = total = 0
        for path in self._quarantine_dir.rglob("*"):
            try:
                st = path.stat()
            except OSError:
                continue
            if stat.S_ISREG(st.st_mode):
                count += 1
                total += st.st_size
        return count, total

    def _evict(self, path: Path, *, reason: str) -> int | None:
        """
        Delete one entry unless it is currently in use. Returns the number of
        bytes freed, or ``None`` when the entry was skipped (missing, locked
        by a producer or held open on Windows).
        """
        lock = self.lock_for(path)
        if not lock.acquire(blocking=False):
            logger.debug("Skipping in-use cache entry during %s: %s", reason, path)
            return None
        try:
            try:
                size = path.stat().st_size
                path.unlink()
            except FileNotFoundError:
                return None
            except PermissionError:
                logger.debug(
                    "Skipping cache entry held open during %s: %s", reason, path
                )
                return None
        finally:
            lock.release()

        self._prune_empty_parent_dirs(path)
        self._record_event(
            action="evict",
            reason=reason,
            entry=self._relative(path),
            bytes=size,
        )
        logger.debug("Evicted cache entry (%s): %s (%d bytes)", reason, path, size)
        return size

    def _prune_empty_parent_dirs(self, path: Path) -> None:
        parent = path.parent.resolve(strict=False)
        while parent != self._root_resolved and parent.is_relative_to(
            self._root_resolved
        ):
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    def _relative(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()

    def _record_event(self, **fields: Any) -> None:
        fields.setdefault("time", round(time.time(), 3))
        fields.setdefault("category", self.category)
        line = json.dumps(fields, ensure_ascii=False, sort_keys=True) + "\n"
        with self._event_lock:
            try:
                self._governance_dir.mkdir(parents=True, exist_ok=True)
                log = self._events_log
                if log.exists() and log.stat().st_size > _EVENTS_LOG_MAX_BYTES:
                    rotated = self._governance_dir / _EVENTS_LOG_ROTATED_NAME
                    rotated.unlink(missing_ok=True)
                    os.replace(log, rotated)
                with log.open("a", encoding="utf-8") as f:
                    f.write(line)
            except OSError:
                logger.debug("Unable to record cache governance event", exc_info=True)
