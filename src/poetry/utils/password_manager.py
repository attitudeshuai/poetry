from __future__ import annotations

import dataclasses
import functools
import logging

from contextlib import suppress
from typing import TYPE_CHECKING

from poetry.config.config import Config
from poetry.utils.secret_redactor import redactor
from poetry.utils.threading import atomic_cached_property


if TYPE_CHECKING:
    from collections.abc import Sequence

    import keyring.backend

    from cleo.io.io import IO


logger = logging.getLogger(__name__)


# Credential source layers, in precedence order.
SOURCE_ENVIRONMENT = "environment"
SOURCE_CONFIG = "config"
SOURCE_KEYRING = "keyring"
SOURCE_URL = "url"

_KEYRING_NAMESPACE = "poetry-repository"


class PoetryKeyringError(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class CredentialLookup:
    """
    A single lookup performed while resolving credentials.

    ``source`` identifies the source layer (environment/config/keyring/url),
    ``target`` what was looked up (environment variable name, config key or
    keyring service), ``hit`` whether it supplied a value and ``reason``
    explains why the lookup did not hit.
    """

    source: str
    target: str
    hit: bool
    reason: str | None = None


@dataclasses.dataclass
class CredentialAudit:
    """Ordered record of every credential lookup with its outcome."""

    lookups: list[CredentialLookup] = dataclasses.field(default_factory=list)

    def record(
        self,
        source: str,
        target: str,
        *,
        hit: bool,
        reason: str | None = None,
    ) -> None:
        self.lookups.append(CredentialLookup(source, target, hit, reason))

    def merge(self, *audits: CredentialAudit) -> CredentialAudit:
        for audit in audits:
            self.lookups.extend(audit.lookups)
        return self

    @property
    def hit(self) -> CredentialLookup | None:
        """The first lookup that supplied a value, if any."""
        return next((lookup for lookup in self.lookups if lookup.hit), None)

    @property
    def source(self) -> str | None:
        """Source layer that supplied the secret value, if any.

        Lookups are recorded in resolution order, so for credentials assembled
        from several layers (e.g. a username from the config file and a password
        from the keyring) this reports the layer of the final, decisive lookup.
        """
        return next(
            (lookup.source for lookup in reversed(self.lookups) if lookup.hit),
            None,
        )

    def describe(self) -> str:
        parts: list[str] = []
        for lookup in self.lookups:
            status = "hit" if lookup.hit else "miss"
            part = f"{lookup.source} ({lookup.target}): {status}"
            if not lookup.hit and lookup.reason:
                part += f" [{lookup.reason}]"
            parts.append(part)
        return " -> ".join(parts)


@dataclasses.dataclass(frozen=True)
class KeyringAvailability:
    """Availability of the system keyring along with a diagnostic reason."""

    available: bool
    backend: str | None = None
    reason: str | None = None


@dataclasses.dataclass
class HTTPAuthCredential:
    username: str | None = dataclasses.field(default=None)
    password: str | None = dataclasses.field(default=None)
    audit: CredentialAudit = dataclasses.field(
        default_factory=CredentialAudit, compare=False, repr=False
    )


class PoetryKeyring:
    # some private sources expect tokens to be provided as passwords with empty usernames
    # we use a fixed literal to ensure that this can be stored in keyring (jaraco/keyring#687)
    #
    # Note: If this is changed, users with passwords stored with empty usernames will have to
    # re-add the config.
    _EMPTY_USERNAME_KEY = "__poetry_source_empty_username__"

    def __init__(self, namespace: str) -> None:
        self._namespace = namespace

    @staticmethod
    def preflight_check(io: IO | None = None, config: Config | None = None) -> None:
        """
        Performs a preflight check to determine the availability of the keyring service
        and logs the status if verbosity is enabled. This method is used to validate
        the configuration setup related to the keyring functionality.

        :param io: An optional input/output handler used to log messages during the
            preflight check. If not provided, logging will be skipped.
        :param config: An optional configuration object. If not provided, a new
            configuration instance will be created using the default factory method.
        :return: None
        """
        config = config or Config.create()

        if config.get("keyring.enabled"):
            if io and io.is_verbose():
                io.write("Checking keyring availability: ")

            message = "<fg=yellow;options=bold>Unavailable</>"

            with suppress(RuntimeError, ValueError):
                if PoetryKeyring.is_available():
                    message = "<fg=green;options=bold>Available</>"

            if io and io.is_verbose():
                io.write(message)
                io.write_line("")

    def get_credential(
        self,
        *names: str,
        username: str | None = None,
        audit: CredentialAudit | None = None,
    ) -> HTTPAuthCredential:
        import keyring

        from keyring.errors import KeyringError
        from keyring.errors import KeyringLocked

        for name in names:
            credential = None
            try:
                # we do default to empty username string here since credentials support empty usernames
                credential = keyring.get_credential(name, username)
            except KeyringLocked:
                logger.debug("Keyring %s is locked", name)
                if audit is not None:
                    audit.record(
                        SOURCE_KEYRING,
                        f"keyring:{name}",
                        hit=False,
                        reason="keyring is locked",
                    )
            except (KeyringError, RuntimeError):
                logger.debug("Accessing keyring %s failed", name, exc_info=True)
                if audit is not None:
                    audit.record(
                        SOURCE_KEYRING,
                        f"keyring:{name}",
                        hit=False,
                        reason="keyring access failed",
                    )
            else:
                if credential:
                    if audit is not None:
                        audit.record(SOURCE_KEYRING, f"keyring:{name}", hit=True)
                    return HTTPAuthCredential(
                        username=credential.username,
                        password=credential.password,
                        audit=audit or CredentialAudit(),
                    )
                if audit is not None:
                    audit.record(
                        SOURCE_KEYRING,
                        f"keyring:{name}",
                        hit=False,
                        reason="no matching entry",
                    )

        return HTTPAuthCredential(
            username=username, password=None, audit=audit or CredentialAudit()
        )

    def get_password(self, name: str, username: str | None) -> str | None:
        import keyring
        import keyring.errors

        name = self.get_entry_name(name)

        try:
            return keyring.get_password(name, username or self._EMPTY_USERNAME_KEY)
        except (RuntimeError, keyring.errors.KeyringError) as e:
            raise PoetryKeyringError(
                f"Unable to retrieve the password for {name} from the key ring "
                f"{self.backend_name()!r}: {e}"
            )

    def set_password(self, name: str, username: str, password: str) -> None:
        import keyring
        import keyring.errors

        name = self.get_entry_name(name)

        try:
            keyring.set_password(name, username or self._EMPTY_USERNAME_KEY, password)
        except (RuntimeError, keyring.errors.KeyringError) as e:
            raise PoetryKeyringError(
                f"Unable to store the password for {name} in the key ring "
                f"{self.backend_name()!r}: {e}"
            )

    def delete_password(self, name: str, username: str) -> None:
        import keyring.errors

        name = self.get_entry_name(name)

        try:
            keyring.delete_password(name, username or self._EMPTY_USERNAME_KEY)
        except (RuntimeError, keyring.errors.KeyringError):
            raise PoetryKeyringError(
                f"Unable to delete the password for {name} from the key ring "
                f"{self.backend_name()!r}"
            )

    def get_entry_name(self, name: str) -> str:
        return f"{self._namespace}-{name}"

    @classmethod
    @functools.cache
    def availability(cls) -> KeyringAvailability:
        """
        Probe the system keyring and report whether it can be used along with
        the active backend name and, when unavailable, the reason.
        """
        logger.debug("Checking if keyring is available")
        try:
            import keyring
            import keyring.backend
            import keyring.errors
        except ImportError as e:
            logger.debug("An error occurred while importing keyring: %s", e)
            return KeyringAvailability(
                available=False, reason="the keyring package is not installed"
            )

        def backend_name(backend: keyring.backend.KeyringBackend) -> str:
            name: str = backend.name
            return name.split(" ")[0]

        def backend_is_valid(backend: keyring.backend.KeyringBackend) -> bool:
            name = backend_name(backend)
            if name in ("chainer", "fail", "null"):
                logger.debug(f"Backend {backend.name!r} is not suitable")
                return False
            elif "plaintext" in backend.name.lower():
                logger.debug(f"Not using plaintext keyring backend {backend.name!r}")
                return False

            return True

        backend = keyring.get_keyring()
        if backend_name(backend) == "chainer":
            backends = keyring.backend.get_all_keyring()
            valid_backend = next((b for b in backends if backend_is_valid(b)), None)
        else:
            valid_backend = backend if backend_is_valid(backend) else None

        if valid_backend is None:
            logger.debug("No valid keyring backend was found")
            return KeyringAvailability(
                available=False,
                backend=backend.name,
                reason=(
                    "no supported keyring backend is available "
                    f"(active backend: {backend.name!r})"
                ),
            )

        logger.debug(f"Using keyring backend {backend.name!r}")

        try:
            # unfortunately there is no clean way of checking if keyring is unlocked
            keyring.get_password("python-poetry-check", "python-poetry")
        except (RuntimeError, keyring.errors.KeyringError):
            logger.debug(
                "Accessing keyring failed during availability check", exc_info=True
            )
            return KeyringAvailability(
                available=False,
                backend=valid_backend.name,
                reason="the keyring backend is locked or not accessible",
            )

        return KeyringAvailability(available=True, backend=valid_backend.name)

    @classmethod
    @functools.cache
    def is_available(cls) -> bool:
        return cls.availability().available

    @staticmethod
    def backend_name() -> str | None:
        """Name of the currently configured keyring backend, if discoverable."""
        with suppress(Exception):
            import keyring

            return str(keyring.get_keyring().name)
        return None


class PasswordManager:
    def __init__(self, config: Config) -> None:
        self._config = config

    @atomic_cached_property
    def use_keyring(self) -> bool:
        return self._config.get("keyring.enabled") and PoetryKeyring.is_available()

    @atomic_cached_property
    def keyring(self) -> PoetryKeyring:
        if not self.use_keyring:
            raise PoetryKeyringError(
                "Access to keyring was requested, but it is not available"
            )

        return PoetryKeyring(_KEYRING_NAMESPACE)

    @staticmethod
    def warn_plaintext_credentials_stored() -> None:
        logger.warning("Using a plaintext file to store credentials")

    def set_pypi_token(self, repo_name: str, token: str) -> None:
        if not self.use_keyring:
            self.warn_plaintext_credentials_stored()
            self._config.auth_config_source.add_property(
                ["pypi-token", repo_name], token
            )
        else:
            self.keyring.set_password(repo_name, "__token__", token)

    def _resolve_pypi_token(self, repo_name: str) -> tuple[str | None, CredentialAudit]:
        """Get PyPi token and an audit of the sources consulted.

        First checks the environment variables for a token,
        then the configured username/password and the
        available keyring.

        :param repo_name:  Name of repository.
        :return: Returns a token as a string if found, otherwise None.
        """
        audit = CredentialAudit()
        keys: list[str] = ["pypi-token", repo_name]
        token: str | None = self._config.get(keys)
        self._record_config_lookup(keys, audit, present=bool(token))

        if not token:
            token = self._lookup_keyring_password(repo_name, "__token__", audit)

        token = token or None
        redactor.register(token)
        self._log_audit(f"pypi-token.{repo_name}", audit)
        return token, audit

    def get_pypi_token(self, repo_name: str) -> str | None:
        """Get PyPi token.

        First checks the environment variables for a token,
        then the configured username/password and the
        available keyring.

        :param repo_name:  Name of repository.
        :return: Returns a token as a string if found, otherwise None.
        """
        return self._resolve_pypi_token(repo_name)[0]

    def delete_pypi_token(self, repo_name: str) -> None:
        if not self.use_keyring:
            return self._config.auth_config_source.remove_property(
                ["pypi-token", repo_name]
            )

        self.keyring.delete_password(repo_name, "__token__")

    def get_http_auth(self, repo_name: str) -> HTTPAuthCredential:
        audit = CredentialAudit()

        username_keys = ["http-basic", repo_name, "username"]
        password_keys = ["http-basic", repo_name, "password"]

        username = self._config.get(username_keys)
        self._record_config_lookup(username_keys, audit, present=username is not None)

        password = self._config.get(password_keys)
        self._record_config_lookup(password_keys, audit, present=password is not None)

        if password is None:
            password = self._lookup_keyring_password(repo_name, username, audit)

        # we use `or None` here to ensure that empty strings are passed as None
        credential = HTTPAuthCredential(
            username=username or None,
            password=password or None,
            audit=audit,
        )
        redactor.register(credential.password)
        self._log_audit(f"http-basic.{repo_name}", audit)
        return credential

    def set_http_password(self, repo_name: str, username: str, password: str) -> None:
        auth = {"username": username}

        if not self.use_keyring:
            self.warn_plaintext_credentials_stored()
            auth["password"] = password
        else:
            self.keyring.set_password(repo_name, username, password)

        self._config.auth_config_source.add_property(["http-basic", repo_name], auth)

    def delete_http_password(self, repo_name: str) -> None:
        auth = self.get_http_auth(repo_name)

        if auth.username is None:
            return

        with suppress(PoetryKeyringError):
            self.keyring.delete_password(repo_name, auth.username)

        self._config.auth_config_source.remove_property(["http-basic", repo_name])

    def get_credential(
        self, *names: str, username: str | None = None
    ) -> HTTPAuthCredential:
        audit = CredentialAudit()

        skip_reason = self._keyring_skip_reason()
        if skip_reason is not None:
            for name in names:
                audit.record(
                    SOURCE_KEYRING,
                    f"keyring:{name}",
                    hit=False,
                    reason=skip_reason,
                )
            self._log_audit("keyring credential fallback", audit)
            return HTTPAuthCredential(username=username, audit=audit)

        credential = self.keyring.get_credential(*names, username=username, audit=audit)
        redactor.register(credential.password)
        self._log_audit("keyring credential fallback", audit)
        return credential

    def _keyring_skip_reason(self) -> str | None:
        """Return the reason the keyring must be skipped, or None when usable."""
        if not self._config.get("keyring.enabled"):
            return "keyring disabled by keyring.enabled configuration"

        availability = PoetryKeyring.availability()
        if availability.available:
            return None

        return availability.reason or "keyring is not available"

    def _lookup_keyring_password(
        self,
        repo_name: str,
        username: str | None,
        audit: CredentialAudit,
    ) -> str | None:
        entry_name = f"{_KEYRING_NAMESPACE}-{repo_name}"
        target = f"keyring:{entry_name}"

        skip_reason = self._keyring_skip_reason()
        if skip_reason is not None:
            audit.record(SOURCE_KEYRING, target, hit=False, reason=skip_reason)
            return None

        try:
            password = self.keyring.get_password(repo_name, username)
        except PoetryKeyringError as e:
            # Declared policy: a keyring read failure must not crash credential
            # resolution. Record the failure and degrade to the remaining sources.
            reason = redactor.redact(str(e))
            logger.warning(
                "Keyring lookup for %s failed; continuing with the remaining "
                "credential sources: %s",
                entry_name,
                reason,
            )
            audit.record(SOURCE_KEYRING, target, hit=False, reason=reason)
            return None

        if password is None:
            audit.record(
                SOURCE_KEYRING,
                target,
                hit=False,
                reason="no matching entry",
            )
        else:
            audit.record(SOURCE_KEYRING, target, hit=True)
            redactor.register(password)

        return password

    def _record_config_lookup(
        self,
        keys: Sequence[str],
        audit: CredentialAudit,
        *,
        present: bool = True,
    ) -> None:
        """Record the environment variable and config file layers for a key."""
        env_name = "POETRY_" + "_".join(key.upper().replace("-", "_") for key in keys)
        config_key = ".".join(keys)
        source = self._config.source_of(keys)

        if source == SOURCE_ENVIRONMENT:
            audit.record(
                SOURCE_ENVIRONMENT,
                env_name,
                hit=present,
                reason=None if present else "environment variable is empty",
            )
            # an existing environment variable shadows the file configuration,
            # even when its value is empty
            audit.record(
                SOURCE_CONFIG,
                config_key,
                hit=False,
                reason="environment variable takes precedence",
            )
        elif source == SOURCE_CONFIG:
            audit.record(
                SOURCE_ENVIRONMENT,
                env_name,
                hit=False,
                reason="environment variable is not set",
            )
            audit.record(
                SOURCE_CONFIG,
                config_key,
                hit=present,
                reason=None if present else "configured value is empty",
            )
        else:
            audit.record(
                SOURCE_ENVIRONMENT,
                env_name,
                hit=False,
                reason="environment variable is not set",
            )
            audit.record(SOURCE_CONFIG, config_key, hit=False, reason="not configured")

    @staticmethod
    def _log_audit(label: str, audit: CredentialAudit) -> None:
        logger.debug("Credential lookup for %s: %s", label, audit.describe())
