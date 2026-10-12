from __future__ import annotations

import logging
import os

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from keyring.credentials import SimpleCredential

from poetry.utils.password_manager import SOURCE_CONFIG
from poetry.utils.password_manager import SOURCE_ENVIRONMENT
from poetry.utils.password_manager import SOURCE_KEYRING
from poetry.utils.password_manager import CredentialAudit
from poetry.utils.password_manager import CredentialLookup
from poetry.utils.password_manager import HTTPAuthCredential
from poetry.utils.password_manager import PasswordManager
from poetry.utils.password_manager import PoetryKeyring
from poetry.utils.password_manager import PoetryKeyringError


if TYPE_CHECKING:
    from pytest import LogCaptureFixture
    from pytest_mock import MockerFixture

    from tests.conftest import Config
    from tests.conftest import DummyBackend


def test_set_http_password(
    config: Config, with_simple_keyring: None, dummy_keyring: DummyBackend
) -> None:
    manager = PasswordManager(config)

    assert PoetryKeyring.is_available()
    manager.set_http_password("foo", "bar", "baz")

    assert dummy_keyring.get_password("poetry-repository-foo", "bar") == "baz"

    auth = config.get("http-basic.foo")
    assert auth["username"] == "bar"
    assert "password" not in auth


@pytest.mark.parametrize(
    ("username", "password", "is_valid"),
    [
        ("bar", "baz", True),
        ("", "baz", True),
        ("bar", "", True),
        ("", "", False),
    ],
)
def test_get_http_auth(
    username: str,
    password: str,
    is_valid: bool,
    config: Config,
    with_simple_keyring: None,
    poetry_keyring: PoetryKeyring,
) -> None:
    poetry_keyring.set_password("foo", username, password)

    config.auth_config_source.add_property("http-basic.foo", {"username": username})
    manager = PasswordManager(config)

    assert PoetryKeyring.is_available()
    auth = manager.get_http_auth("foo")

    if is_valid:
        assert auth is not None
        assert auth.username == (username or None)
        assert auth.password == (password or None)
    else:
        assert auth.username is auth.password is None


def test_delete_http_password(
    config: Config, with_simple_keyring: None, dummy_keyring: DummyBackend
) -> None:
    dummy_keyring.set_password("poetry-repository-foo", "bar", "baz")
    config.auth_config_source.add_property("http-basic.foo", {"username": "bar"})
    manager = PasswordManager(config)

    assert PoetryKeyring.is_available()
    manager.delete_http_password("foo")

    assert dummy_keyring.get_password("poetry-repository-foo", "bar") is None
    assert config.get("http-basic.foo") is None


def test_set_pypi_token(
    config: Config, with_simple_keyring: None, dummy_keyring: DummyBackend
) -> None:
    manager = PasswordManager(config)

    assert PoetryKeyring.is_available()
    manager.set_pypi_token("foo", "baz")

    assert config.get("pypi-token.foo") is None

    assert dummy_keyring.get_password("poetry-repository-foo", "__token__") == "baz"


def test_get_pypi_token(
    config: Config, with_simple_keyring: None, dummy_keyring: DummyBackend
) -> None:
    dummy_keyring.set_password("poetry-repository-foo", "__token__", "baz")
    manager = PasswordManager(config)

    assert PoetryKeyring.is_available()
    assert manager.get_pypi_token("foo") == "baz"


def test_delete_pypi_token(
    config: Config, with_simple_keyring: None, dummy_keyring: DummyBackend
) -> None:
    dummy_keyring.set_password("poetry-repository-foo", "__token__", "baz")
    manager = PasswordManager(config)

    assert PoetryKeyring.is_available()
    manager.delete_pypi_token("foo")

    assert dummy_keyring.get_password("poetry-repository-foo", "__token__") is None


def test_set_http_password_with_unavailable_backend(
    config: Config, with_fail_keyring: None
) -> None:
    manager = PasswordManager(config)

    assert not PoetryKeyring.is_available()
    manager.set_http_password("foo", "bar", "baz")

    auth = config.get("http-basic.foo")
    assert auth["username"] == "bar"
    assert auth["password"] == "baz"


@pytest.mark.parametrize(
    ("username", "password", "is_valid"),
    [
        ("bar", "baz", True),
        ("", "baz", True),
        ("bar", "", True),
        ("", "", False),
    ],
)
def test_get_http_auth_with_unavailable_backend(
    username: str,
    password: str,
    is_valid: bool,
    config: Config,
    with_fail_keyring: None,
) -> None:
    config.auth_config_source.add_property(
        "http-basic.foo", {"username": username, "password": password}
    )
    manager = PasswordManager(config)

    assert not PoetryKeyring.is_available()
    auth = manager.get_http_auth("foo")

    if is_valid:
        assert auth is not None
        assert auth.username == (username or None)
        assert auth.password == (password or None)
    else:
        assert auth.username is auth.password is None


def test_delete_http_password_with_unavailable_backend(
    config: Config, with_fail_keyring: None
) -> None:
    config.auth_config_source.add_property(
        "http-basic.foo", {"username": "bar", "password": "baz"}
    )
    manager = PasswordManager(config)

    assert not PoetryKeyring.is_available()
    manager.delete_http_password("foo")

    assert config.get("http-basic.foo") is None


def test_set_pypi_token_with_unavailable_backend(
    config: Config, with_fail_keyring: None
) -> None:
    manager = PasswordManager(config)

    assert not PoetryKeyring.is_available()
    manager.set_pypi_token("foo", "baz")

    assert config.get("pypi-token.foo") == "baz"


def test_get_pypi_token_with_unavailable_backend(
    config: Config, with_fail_keyring: None
) -> None:
    config.auth_config_source.add_property("pypi-token.foo", "baz")
    manager = PasswordManager(config)

    assert not PoetryKeyring.is_available()
    assert manager.get_pypi_token("foo") == "baz"


def test_delete_pypi_token_with_unavailable_backend(
    config: Config, with_fail_keyring: None
) -> None:
    config.auth_config_source.add_property("pypi-token.foo", "baz")
    manager = PasswordManager(config)

    assert not PoetryKeyring.is_available()
    manager.delete_pypi_token("foo")

    assert config.get("pypi-token.foo") is None


def test_keyring_raises_errors_on_keyring_errors(
    mocker: MockerFixture, with_fail_keyring: None
) -> None:
    mocker.patch("poetry.utils.password_manager.PoetryKeyring.is_available")

    key_ring = PoetryKeyring("poetry")
    with pytest.raises(PoetryKeyringError):
        key_ring.set_password("foo", "bar", "baz")

    with pytest.raises(PoetryKeyringError):
        key_ring.get_password("foo", "bar")

    with pytest.raises(PoetryKeyringError):
        key_ring.delete_password("foo", "bar")


def test_keyring_returns_none_on_locked_keyring(
    with_locked_keyring: None,
    caplog: LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="poetry.utils.password_manager")
    key_ring = PoetryKeyring("poetry")

    cred = key_ring.get_credential("foo")

    assert cred.password is None
    assert "Keyring foo is locked" in caplog.messages


def test_keyring_returns_none_on_erroneous_keyring(
    with_erroneous_keyring: None,
    caplog: LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="poetry.utils.password_manager")
    key_ring = PoetryKeyring("poetry")

    cred = key_ring.get_credential("foo")

    assert cred.password is None
    assert "Accessing keyring foo failed" in caplog.messages


def test_keyring_with_chainer_backend_and_fail_keyring_should_be_unavailable(
    with_chained_fail_keyring: None,
) -> None:
    key_ring = PoetryKeyring("poetry")

    assert not key_ring.is_available()


def test_keyring_with_chainer_backend_and_null_keyring_should_be_unavailable(
    with_chained_null_keyring: None,
) -> None:
    key_ring = PoetryKeyring("poetry")

    assert not key_ring.is_available()


def test_null_keyring_should_be_unavailable(
    with_null_keyring: None,
) -> None:
    key_ring = PoetryKeyring("poetry")

    assert not key_ring.is_available()


def test_fail_keyring_should_be_unavailable(
    with_fail_keyring: None,
) -> None:
    key_ring = PoetryKeyring("poetry")

    assert not key_ring.is_available()


def test_locked_keyring_should_not_be_available(with_locked_keyring: None) -> None:
    key_ring = PoetryKeyring("poetry")

    assert not key_ring.is_available()


def test_erroneous_keyring_should_not_be_available(
    with_erroneous_keyring: None,
) -> None:
    key_ring = PoetryKeyring("poetry")

    assert not key_ring.is_available()


def test_get_http_auth_from_environment_variables(
    environ: None, config: Config
) -> None:
    os.environ["POETRY_HTTP_BASIC_FOO_USERNAME"] = "bar"
    os.environ["POETRY_HTTP_BASIC_FOO_PASSWORD"] = "baz"

    manager = PasswordManager(config)

    auth = manager.get_http_auth("foo")
    assert auth == HTTPAuthCredential(username="bar", password="baz")


def test_get_http_auth_does_not_call_keyring_when_credentials_in_environment_variables(
    environ: None, config: Config
) -> None:
    os.environ["POETRY_HTTP_BASIC_FOO_USERNAME"] = "bar"
    os.environ["POETRY_HTTP_BASIC_FOO_PASSWORD"] = "baz"

    manager = PasswordManager(config)
    manager.keyring = MagicMock()

    auth = manager.get_http_auth("foo")
    assert auth == HTTPAuthCredential(username="bar", password="baz")
    manager.keyring.get_password.assert_not_called()


def test_get_http_auth_does_not_call_keyring_when_password_in_environment_variables(
    environ: None, config: Config
) -> None:
    config.merge(
        {
            "http-basic": {"foo": {"username": "bar"}},
        }
    )
    os.environ["POETRY_HTTP_BASIC_FOO_PASSWORD"] = "baz"

    manager = PasswordManager(config)
    manager.keyring = MagicMock()

    auth = manager.get_http_auth("foo")
    assert auth == HTTPAuthCredential(username="bar", password="baz")
    manager.keyring.get_password.assert_not_called()


def test_get_pypi_token_with_env_var_positive(
    mocker: MockerFixture,
    config: Config,
    with_simple_keyring: None,
    dummy_keyring: DummyBackend,
) -> None:
    sample_token = "sampletoken-1234"
    repo_name = "foo"
    manager = PasswordManager(config)
    mocker.patch.dict(
        os.environ,
        {f"POETRY_PYPI_TOKEN_{repo_name.upper()}": sample_token},
    )

    assert manager.get_pypi_token(repo_name) == sample_token


def test_get_pypi_token_with_env_var_not_available(
    config: Config, with_simple_keyring: None, dummy_keyring: DummyBackend
) -> None:
    repo_name = "foo"
    manager = PasswordManager(config)

    result_token = manager.get_pypi_token(repo_name)

    assert result_token is None


def test_disabled_keyring_never_called(
    config: Config, with_simple_keyring: None, dummy_keyring: DummyBackend
) -> None:
    config.config["keyring"]["enabled"] = False
    config.config["http-basic"] = {"onlyuser": {"username": "user"}}

    manager = PasswordManager(config)
    num_public_functions = len([f for f in dir(manager) if not f.startswith("_")])
    if num_public_functions != 10:
        pytest.fail(
            f"A function was added to or removed from the {PasswordManager.__name__} "
            "class without reflecting this change in this test."
        )

    with pytest.raises(PoetryKeyringError) as e:
        _ = manager.keyring

    assert str(e.value) == "Access to keyring was requested, but it is not available"

    # We made sure that accessing a disabled keyring raises an exception.
    # Now we call the PasswordManager functions that do access the keyring to
    # make sure that they never do so when the keyring is disabled.
    manager.set_pypi_token(repo_name="exists", token="token")
    manager.get_pypi_token(repo_name="exists")
    manager.get_pypi_token(repo_name="doesn't exist")
    manager.delete_pypi_token(repo_name="exists")
    manager.delete_pypi_token(repo_name="doesn't exist")
    manager.set_http_password(repo_name="exists", username="user", password="password")
    manager.get_http_auth(repo_name="exists")
    manager.get_http_auth(repo_name="doesn't exist")
    manager.get_http_auth(repo_name="onlyuser")
    manager.delete_http_password(repo_name="exits")
    manager.delete_http_password(repo_name="doesn't exist")
    manager.delete_http_password(repo_name="onlyuser")
    manager.get_credential("a", "b", "c", username="user")


def test_http_auth_audit_reports_environment_source(
    environ: None, config: Config
) -> None:
    os.environ["POETRY_HTTP_BASIC_FOO_USERNAME"] = "bar"
    os.environ["POETRY_HTTP_BASIC_FOO_PASSWORD"] = "secret-password"

    auth = PasswordManager(config).get_http_auth("foo")

    assert auth == HTTPAuthCredential(username="bar", password="secret-password")
    assert auth.audit.source == SOURCE_ENVIRONMENT
    assert auth.audit.hit == CredentialLookup(
        SOURCE_ENVIRONMENT, "POETRY_HTTP_BASIC_FOO_USERNAME", True, None
    )
    targets = [(lookup.source, lookup.target) for lookup in auth.audit.lookups]
    assert targets[:2] == [
        (SOURCE_ENVIRONMENT, "POETRY_HTTP_BASIC_FOO_USERNAME"),
        (SOURCE_CONFIG, "http-basic.foo.username"),
    ]
    # the config layer was skipped because the environment variable wins
    config_lookup = next(
        lookup
        for lookup in auth.audit.lookups
        if lookup.target == "http-basic.foo.username"
    )
    assert config_lookup.reason == "environment variable takes precedence"


def test_http_auth_audit_reports_config_source_with_miss_reasons(
    config: Config, with_fail_keyring: None
) -> None:
    config.auth_config_source.add_property(
        "http-basic.foo", {"username": "bar", "password": "secret-password"}
    )

    auth = PasswordManager(config).get_http_auth("foo")

    assert auth.audit.source == SOURCE_CONFIG
    env_lookups = [
        lookup for lookup in auth.audit.lookups if lookup.source == SOURCE_ENVIRONMENT
    ]
    assert env_lookups and all(
        lookup.reason == "environment variable is not set" and not lookup.hit
        for lookup in env_lookups
    )


def test_http_auth_audit_reports_keyring_source(
    config: Config,
    with_simple_keyring: None,
    poetry_keyring: PoetryKeyring,
) -> None:
    poetry_keyring.set_password("foo", "bar", "keyring-secret")
    config.auth_config_source.add_property("http-basic.foo", {"username": "bar"})

    auth = PasswordManager(config).get_http_auth("foo")

    assert auth.password == "keyring-secret"
    assert auth.audit.source == SOURCE_KEYRING
    keyring_hit = next(
        lookup
        for lookup in auth.audit.lookups
        if lookup.source == SOURCE_KEYRING and lookup.hit
    )
    assert keyring_hit.target == "keyring:poetry-repository-foo"
    password_config = next(
        lookup
        for lookup in auth.audit.lookups
        if lookup.target == "http-basic.foo.password"
    )
    assert not password_config.hit
    assert password_config.reason == "not configured"


def test_http_auth_audit_records_keyring_disabled(
    config: Config,
) -> None:
    config.config["keyring"]["enabled"] = False
    config.auth_config_source.add_property("http-basic.foo", {"username": "bar"})

    auth = PasswordManager(config).get_http_auth("foo")

    keyring_lookups = [
        lookup for lookup in auth.audit.lookups if lookup.source == SOURCE_KEYRING
    ]
    assert len(keyring_lookups) == 1
    assert not keyring_lookups[0].hit
    assert "keyring.enabled" in (keyring_lookups[0].reason or "")


def test_http_auth_degrades_when_keyring_read_fails(
    config: Config,
    with_simple_keyring: None,
    caplog: LogCaptureFixture,
    mocker: MockerFixture,
) -> None:
    config.auth_config_source.add_property("http-basic.foo", {"username": "bar"})
    mocker.patch.object(
        PoetryKeyring,
        "get_password",
        side_effect=PoetryKeyringError("backend exploded"),
    )
    manager = PasswordManager(config)

    auth = manager.get_http_auth("foo")

    assert auth.username == "bar"
    assert auth.password is None
    keyring_lookup = next(
        lookup for lookup in auth.audit.lookups if lookup.source == SOURCE_KEYRING
    )
    assert not keyring_lookup.hit
    assert "backend exploded" in (keyring_lookup.reason or "")
    assert "continuing with the remaining credential sources" in caplog.text


def test_keyring_credential_fallback_audit_records_each_service(
    config: Config, with_erroneous_keyring: None
) -> None:
    manager = PasswordManager(config)

    credential = manager.get_credential(
        "https://foo.bar/simple/", "foo.bar", username="bar"
    )

    assert credential.password is None
    keyring_lookups = [
        lookup for lookup in credential.audit.lookups if lookup.source == SOURCE_KEYRING
    ]
    assert [lookup.target for lookup in keyring_lookups] == [
        "keyring:https://foo.bar/simple/",
        "keyring:foo.bar",
    ]
    assert all(lookup.reason for lookup in keyring_lookups)


def test_keyring_credential_fallback_audit_records_hit(
    config: Config, with_simple_keyring: None, dummy_keyring: DummyBackend
) -> None:
    dummy_keyring.set_default_service_credential(
        "foo.bar", SimpleCredential("foo", "keyring-secret")
    )

    credential = PasswordManager(config).get_credential(
        "https://foo.bar/simple/", "foo.bar"
    )

    assert credential.password == "keyring-secret"
    assert credential.audit.source == SOURCE_KEYRING
    assert credential.audit.hit is not None
    assert credential.audit.hit.target == "keyring:foo.bar"


def test_pypi_token_audit_reports_environment_source(
    environ: None, config: Config
) -> None:
    os.environ["POETRY_PYPI_TOKEN_FOO"] = "env-token-value"

    token, audit = PasswordManager(config)._resolve_pypi_token("foo")

    assert token == "env-token-value"
    assert audit.source == SOURCE_ENVIRONMENT
    assert audit.hit is not None
    assert audit.hit.target == "POETRY_PYPI_TOKEN_FOO"


def test_pypi_token_audit_reports_keyring_source(
    config: Config,
    with_simple_keyring: None,
    dummy_keyring: DummyBackend,
) -> None:
    dummy_keyring.set_password(
        "poetry-repository-foo", "__token__", "keyring-token-value"
    )

    token, audit = PasswordManager(config)._resolve_pypi_token("foo")

    assert token == "keyring-token-value"
    assert audit.source == SOURCE_KEYRING
    assert audit.hit is not None
    assert audit.hit.target == "keyring:poetry-repository-foo"


def test_availability_reports_reason_for_unavailable_backends(
    with_locked_keyring: None,
) -> None:
    availability = PoetryKeyring.availability()

    assert not availability.available
    assert availability.backend is not None
    assert availability.reason is not None


def test_http_auth_audit_is_logged(
    config: Config,
    with_simple_keyring: None,
    poetry_keyring: PoetryKeyring,
    caplog: LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="poetry.utils.password_manager")
    poetry_keyring.set_password("foo", "bar", "keyring-secret")
    config.auth_config_source.add_property("http-basic.foo", {"username": "bar"})

    PasswordManager(config).get_http_auth("foo")

    audit_logs = [m for m in caplog.messages if m.startswith("Credential lookup")]
    assert audit_logs
    assert "keyring:poetry-repository-foo" in audit_logs[-1]
    assert "keyring-secret" not in audit_logs[-1]


def test_keyring_get_credential_records_locked_reason(
    with_simple_keyring: None, mocker: MockerFixture
) -> None:
    from keyring.errors import KeyringLocked

    mocker.patch("keyring.get_credential", side_effect=KeyringLocked())
    audit = CredentialAudit()

    credential = PoetryKeyring("poetry").get_credential("foo", audit=audit)

    assert credential.password is None
    assert audit.lookups == [
        CredentialLookup(SOURCE_KEYRING, "keyring:foo", False, "keyring is locked")
    ]


def test_keyring_get_credential_without_audit_keeps_behaviour(
    with_simple_keyring: None,
    dummy_keyring: DummyBackend,
) -> None:
    dummy_keyring.set_default_service_credential(
        "foo", SimpleCredential("user", "secret")
    )

    credential = PoetryKeyring("poetry").get_credential("foo")

    assert credential == HTTPAuthCredential(username="user", password="secret")
