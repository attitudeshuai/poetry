from __future__ import annotations

import json

from pathlib import Path
from typing import TYPE_CHECKING
from typing import ClassVar

from cleo.helpers import option

from poetry.config.config import Config
from poetry.console.commands.command import Command
from poetry.utils.cache import CacheGovernor
from poetry.utils.cache import format_size


if TYPE_CHECKING:
    from cleo.io.inputs.option import Option
    from cleo.ui.table import Rows

    from poetry.utils.cache import CacheStats as CacheStatsType


class CacheStatsCommand(Command):
    name = "cache stats"
    description = "Show usage statistics of Poetry's caches."

    options: ClassVar[list[Option]] = [
        option("json", None, "Print the statistics as JSON."),
    ]

    def handle(self) -> int:
        config = Config.create()
        stats = [
            CacheGovernor.for_category(config, category).stats()
            for category in CacheGovernor.CATEGORIES
        ]

        if self.option("json"):
            self.line(json.dumps(self._payload(config, stats), indent=2))
            return 0

        cache_dir = str(Path(config.get("cache-dir")).expanduser())
        self.line(f"Cache directory: {cache_dir}")
        table = self.table()
        table.set_headers(
            [
                "Category",
                "Entries",
                "Size",
                "Expired",
                "Max size",
                "Policy",
                "Quarantined",
            ]
        )
        rows: Rows = [
            [
                item.category,
                str(item.entries),
                format_size(item.size),
                str(item.expired_entries),
                format_size(item.max_size) if item.max_size is not None else "-",
                str(item.policy) if item.policy is not None else "-",
                str(item.quarantined_entries),
            ]
            for item in stats
        ]
        rows.append(
            [
                "total",
                str(sum(item.entries for item in stats)),
                format_size(sum(item.size for item in stats)),
                str(sum(item.expired_entries for item in stats)),
                "",
                "",
                str(sum(item.quarantined_entries for item in stats)),
            ]
        )
        table.set_rows(rows)
        table.render()
        self.line(
            "Quarantine: "
            f"{sum(item.quarantined_entries for item in stats)} entries "
            f"({format_size(sum(item.quarantined_size for item in stats))})"
        )
        return 0

    def _payload(
        self, config: Config, stats: list[CacheStatsType]
    ) -> dict[str, object]:
        return {
            "cache_dir": str(Path(config.get("cache-dir")).expanduser()),
            "categories": [item.as_dict() for item in stats],
            "total": {
                "entries": sum(item.entries for item in stats),
                "size": sum(item.size for item in stats),
                "expired_entries": sum(item.expired_entries for item in stats),
                "quarantined_entries": sum(item.quarantined_entries for item in stats),
                "quarantined_size": sum(item.quarantined_size for item in stats),
            },
        }
