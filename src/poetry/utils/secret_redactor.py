from __future__ import annotations

import re
import threading

from typing import TYPE_CHECKING


if TYPE_CHECKING:
    from collections.abc import Iterable


#: Placeholder substituted for any masked secret.
MASKED = "[REDACTED]"

#: Secrets shorter than this many characters are not registered for value
#: based masking. Replacing very short values would corrupt unrelated output;
#: passwords embedded in URLs are still masked by the structural URL rule.
MIN_SECRET_LENGTH = 4

#: Matches the userinfo part of a URL, e.g. ``https://user:pass@host/path``.
#: The password group is anything between the first colon and the at sign.
#: Percent-encoded passwords (``p%40ss``) are covered as well.
_URL_USERINFO_RE = re.compile(
    r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.\-]*://)"
    r"(?P<username>[^:/@\s'\"<>]+)"
    r":(?P<password>[^@/\s'\"<>]+)@"
)

# A pattern that never matches anything, used before any secret was registered.
_NEVER_MATCH_RE = re.compile(r"(?!x)x")


class SecretRedactor:
    """
    Process-wide registry of secrets with a single, thread-safe masking policy.

    All output destined for the terminal or log files should be passed through
    :meth:`redact`. Credentials resolved by any worker thread register their
    secret values here so that concurrent requests share exactly the same
    masking rules.

    Two kinds of masking are applied:

    1. *structural* masking of the password component in ``scheme://user:pass@``
       URL userinfo, which does not require the password to be known, and
    2. *value* based masking of every registered secret literal (longest first).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._secrets: set[str] = set()
        self._pattern: re.Pattern[str] = _NEVER_MATCH_RE

    def register(self, secret: str | None) -> None:
        """Register a secret value for value based masking.

        Empty values and very short secrets are ignored to avoid corrupting
        unrelated output. Registration is thread-safe.
        """
        if not secret or len(secret) < MIN_SECRET_LENGTH:
            return

        with self._lock:
            if secret in self._secrets:
                return
            self._secrets.add(secret)
            self._pattern = self._compile(self._secrets)

    def register_all(self, secrets: Iterable[str | None]) -> None:
        for secret in secrets:
            self.register(secret)

    def redact(self, text: str | None) -> str:
        """Return ``text`` with URL passwords and registered secrets masked."""
        if not text:
            return ""

        redacted = self._redact_url_userinfo(text)

        pattern = self._pattern
        if pattern is not _NEVER_MATCH_RE:
            redacted = pattern.sub(MASKED, redacted)

        return redacted

    def redact_url(self, url: str | None) -> str:
        """Mask only the password embedded in a URL (identity if absent)."""
        if not url:
            return ""
        return self._redact_url_userinfo(url)

    def reset(self) -> None:
        """Drop all registered secrets (mainly useful in tests)."""
        with self._lock:
            self._secrets.clear()
            self._pattern = _NEVER_MATCH_RE

    @staticmethod
    def _compile(secrets: set[str]) -> re.Pattern[str]:
        # Escape and sort longest first so that a longer secret wins over a
        # shorter one that happens to be a substring of it.
        alternatives = sorted(
            (re.escape(secret) for secret in secrets), key=len, reverse=True
        )
        return re.compile("|".join(alternatives))

    @staticmethod
    def _replace_userinfo(match: re.Match[str]) -> str:
        return f"{match.group('scheme')}{match.group('username')}:{MASKED}@"

    def _redact_url_userinfo(self, text: str) -> str:
        return _URL_USERINFO_RE.sub(self._replace_userinfo, text)


#: Process-wide redactor shared by all authenticators, workers and commands.
redactor = SecretRedactor()
