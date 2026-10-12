from __future__ import annotations

import logging

from unittest.mock import MagicMock

from poetry.console.logging.io_handler import IOHandler
from poetry.utils.secret_redactor import MASKED
from poetry.utils.secret_redactor import redactor


def _emit(message: str, level: int = logging.INFO) -> str:
    io = MagicMock()
    handler = IOHandler(io)
    handler.setFormatter(logging.Formatter("%(message)s"))
    record = logging.LogRecord("poetry.test", level, __file__, 1, message, (), None)
    handler.emit(record)

    written = io.write_line.call_args or io.write_error_line.call_args
    return str(written[0][0])


def test_io_handler_redacts_registered_secret() -> None:
    redactor.register("super-secret-token")

    output = _emit("using credential super-secret-token now")

    assert "super-secret-token" not in output
    assert MASKED in output


def test_io_handler_redacts_url_password() -> None:
    output = _emit("GET https://user:password%401@foo.bar/simple/ ok")

    assert "password%401" not in output
    assert f"https://user:{MASKED}@foo.bar" in output


def test_io_handler_passes_benign_messages_through() -> None:
    assert _emit("Downloading: https://foo.bar/a.whl") == (
        "Downloading: https://foo.bar/a.whl"
    )
