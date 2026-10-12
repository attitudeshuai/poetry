from __future__ import annotations

import os

from typing import TYPE_CHECKING
from typing import ClassVar

from cleo.helpers import argument
from cleo.helpers import option
from packaging.utils import canonicalize_name

from poetry.config.config import Config
from poetry.console.commands.command import Command
from poetry.utils.cache import CacheGovernor
from poetry.utils.cache import CleanupResult
from poetry.utils.cache import FileCache
from poetry.utils.cache import format_size
from poetry.utils.cache import parse_duration
from poetry.utils.cache import parse_size


if TYPE_CHECKING:
    from cleo.io.inputs.argument import Argument
    from cleo.io.inputs.option import Option


_SELECTIVE_OPTIONS = (
    "category",
    "older-than",
    "larger-than",
    "shrink-to",
    "dry-run",
)


class CacheClearCommand(Command):
    name = "cache clear"
    description = "Clear Poetry's caches."

    arguments: ClassVar[list[Argument]] = [
        argument("cache", description="The name of the cache to clear.", optional=True)
    ]
    options: ClassVar[list[Option]] = [
        option("all", description="Clear all entries in the cache."),
        option(
            "category",
            None,
            "Apply the command to the given cache category (artifacts or"
            " repositories). Without this option selective cleanup applies to"
            " all categories.",
            flag=False,
        ),
        option(
            "older-than",
            None,
            "Only remove entries that have not been used for the given"
            " duration, e.g. 30d (units: s, m, h, d, w).",
            flag=False,
        ),
        option(
            "larger-than",
            None,
            "Only remove entries of at least the given size, e.g. 50MB.",
            flag=False,
        ),
        option(
            "shrink-to",
            None,
            "Evict least recently used entries until the category uses at"
            " most the given size, e.g. 200MB.",
            flag=False,
        ),
        option(
            "dry-run",
            None,
            "Show which entries would be removed without removing them.",
        ),
    ]

    def handle(self) -> int:
        if any(self.option(name) for name in _SELECTIVE_OPTIONS):
            return self._handle_selective()

        cache = self.argument("cache")

        if cache:
            parts = cache.split(":")
            root = parts[0]
        else:
            parts = []
            root = ""

        config = Config.create()
        cache_dir = config.repository_cache_directory / root

        try:
            cache_dir.relative_to(config.repository_cache_directory)
        except ValueError:
            raise ValueError(f"{root} is not a valid repository cache")

        cache = FileCache(cache_dir)

        if len(parts) < 2:
            if not self.option("all"):
                raise RuntimeError(
                    "Add the --all option if you want to clear all cache entries"
                )

            if not cache_dir.exists():
                self.line(
                    f"No cache entries for {root}" if root else "No cache entries"
                )
                return 0

            # Calculate number of entries
            entries_count = sum(
                len(files) for _path, _dirs, files in os.walk(str(cache_dir))
            )

            delete = self.confirm(f"<question>Delete {entries_count} entries?</>", True)
            if not delete:
                return 0

            cache.flush()
        elif len(parts) == 2:
            raise RuntimeError(
                "Only specifying the package name is not yet supported. "
                "Add a specific version to clear"
            )
        elif len(parts) == 3:
            package = canonicalize_name(parts[1])
            version = parts[2]

            if not cache.has(f"{package}:{version}"):
                self.line(f"No cache entries for {package}:{version}")
                return 0

            delete = self.confirm(f"Delete cache entry {package}:{version}", True)
            if not delete:
                return 0

            cache.forget(f"{package}:{version}")
        else:
            raise ValueError("Invalid cache key")

        return 0

    def _handle_selective(self) -> int:
        if self.argument("cache"):
            raise ValueError(
                "A repository cache name cannot be combined with --category,"
                " --older-than, --larger-than, --shrink-to or --dry-run."
            )

        if not self.option("all"):
            raise RuntimeError(
                "Add the --all option if you want to clear all cache entries"
            )

        category = self.option("category")
        if category is not None and category not in CacheGovernor.CATEGORIES:
            raise ValueError(
                f"Unknown cache category '{category}'. Valid categories: "
                f"{', '.join(CacheGovernor.CATEGORIES)}."
            )
        categories = (category,) if category is not None else CacheGovernor.CATEGORIES

        older_than = (
            parse_duration(self.option("older-than"))
            if self.option("older-than") is not None
            else None
        )
        larger_than = (
            parse_size(self.option("larger-than"))
            if self.option("larger-than") is not None
            else None
        )
        shrink_to = (
            parse_size(self.option("shrink-to"))
            if self.option("shrink-to") is not None
            else None
        )
        dry_run = bool(self.option("dry-run"))
        whole_category = (
            older_than is None and larger_than is None and shrink_to is None
        )

        config = Config.create()

        # First pass: plan (the dry run also provides the confirmation count)
        plans: list[tuple[str, CleanupResult]] = []
        for name in categories:
            governor = CacheGovernor.for_category(config, name)
            if whole_category:
                current_stats = governor.stats()
                result = CleanupResult(current_stats.entries, current_stats.size, 0, 0)
            else:
                result = governor.cleanup(
                    older_than=older_than,
                    larger_than=larger_than,
                    shrink_to=shrink_to,
                    dry_run=True,
                )
            plans.append((name, result))

        matched = sum(result.matched_entries for _name, result in plans)
        matched_bytes = sum(result.matched_bytes for _name, result in plans)
        if matched == 0:
            self.line("No cache entries matched the given criteria")
            return 0

        if dry_run:
            for name, result in plans:
                if result.matched_entries:
                    self.line(
                        f"{name}: would delete {result.matched_entries} entries"
                        f" ({format_size(result.matched_bytes)})"
                    )
            self.line(f"Would delete {matched} entries ({format_size(matched_bytes)})")
            return 0

        delete = self.confirm(
            f"<question>Delete {matched} entries ({format_size(matched_bytes)})?</>",
            True,
        )
        if not delete:
            return 0

        deleted = freed = 0
        for name, _result in plans:
            governor = CacheGovernor.for_category(config, name)
            if whole_category:
                outcome = governor.flush_entries()
            else:
                outcome = governor.cleanup(
                    older_than=older_than,
                    larger_than=larger_than,
                    shrink_to=shrink_to,
                )
            deleted += outcome.deleted_entries
            freed += outcome.freed_bytes
            if outcome.deleted_entries < outcome.matched_entries:
                self.line(
                    f"{name}: {outcome.deleted_entries} of"
                    f" {outcome.matched_entries} matching entries were removed;"
                    " the rest are in use by another process"
                )

        self.line(f"Deleted {deleted} entries ({format_size(freed)})")
        return 0
