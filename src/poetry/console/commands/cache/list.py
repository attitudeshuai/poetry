from __future__ import annotations

from poetry.config.config import Config
from poetry.console.commands.command import Command


class CacheListCommand(Command):
    name = "cache list"
    description = "List Poetry's caches."

    def handle(self) -> int:
        config = Config.create()
        if config.repository_cache_directory.exists():
            # Hidden directories (e.g. the cache governance data directory)
            # are not repository caches and must not be listed.
            caches = sorted(
                entry.name
                for entry in config.repository_cache_directory.iterdir()
                if entry.is_dir() and not entry.name.startswith(".")
            )
            if caches:
                for cache in caches:
                    self.line(f"<info>{cache}</>")
                return 0

        self.line_error("<warning>No caches found</>")
        return 0
