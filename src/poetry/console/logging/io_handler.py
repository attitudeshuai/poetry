from __future__ import annotations

import logging

from typing import TYPE_CHECKING

from poetry.utils.secret_redactor import redactor


if TYPE_CHECKING:
    from logging import LogRecord

    from cleo.io.io import IO


class IOHandler(logging.Handler):
    def __init__(self, io: IO) -> None:
        self._io = io

        super().__init__()

    def emit(self, record: LogRecord) -> None:
        try:
            # Every log line written to the terminal goes through the shared
            # secret redactor: registered credentials/tokens and passwords
            # embedded in URLs are replaced by placeholders.
            msg = redactor.redact(self.format(record))
            level = record.levelname.lower()
            err = level in ("warning", "error", "exception", "critical")
            if err:
                self._io.write_error_line(msg)
            else:
                self._io.write_line(msg)
        except Exception:  # noqa: BLE001
            self.handleError(record)
