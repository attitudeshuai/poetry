from __future__ import annotations

import threading

from poetry.utils.secret_redactor import MASKED
from poetry.utils.secret_redactor import SecretRedactor
from poetry.utils.secret_redactor import redactor


def test_redact_registered_secret() -> None:
    masker = SecretRedactor()
    masker.register("super-secret-token")

    assert (
        masker.redact("authorization: super-secret-token") == f"authorization: {MASKED}"
    )


def test_redact_nothing_registered_is_identity() -> None:
    masker = SecretRedactor()
    text = "everything stays the same"

    assert masker.redact(text) == text


def test_redact_empty_input() -> None:
    assert SecretRedactor().redact(None) == ""
    assert SecretRedactor().redact("") == ""


def test_short_secrets_are_not_registered() -> None:
    masker = SecretRedactor()
    masker.register("abc")

    assert masker.redact("abc and more text") == "abc and more text"


def test_longer_secret_takes_precedence() -> None:
    masker = SecretRedactor()
    masker.register("secret")
    masker.register("super-secret-value")

    assert masker.redact("x super-secret-value y") == f"x {MASKED} y"


def test_redact_url_userinfo() -> None:
    masker = SecretRedactor()

    assert (
        masker.redact_url("https://user:password@foo.bar/simple/")
        == f"https://user:{MASKED}@foo.bar/simple/"
    )


def test_redact_url_userinfo_with_special_characters() -> None:
    masker = SecretRedactor()

    assert (
        masker.redact_url("https://user:p%40ss%3Aword@foo.bar/simple/")
        == f"https://user:{MASKED}@foo.bar/simple/"
    )


def test_redact_url_without_password_is_identity() -> None:
    masker = SecretRedactor()
    url = "https://user@foo.bar/simple/"

    assert masker.redact_url(url) == url
    assert masker.redact_url("https://foo.bar/simple/") == "https://foo.bar/simple/"


def test_redact_structural_url_without_registered_secret() -> None:
    masker = SecretRedactor()

    assert (
        masker.redact("GET https://user:password@foo.bar/path failed")
        == f"GET https://user:{MASKED}@foo.bar/path failed"
    )


def test_redactor_is_shared_and_thread_safe() -> None:
    def register(value: str) -> None:
        redactor.register(value)

    threads = [
        threading.Thread(target=register, args=(f"secret-value-{i}",))
        for i in range(50)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    for i in range(50):
        assert redactor.redact(f"leak secret-value-{i}") == f"leak {MASKED}"


def test_reset_clears_secrets() -> None:
    masker = SecretRedactor()
    masker.register("remember-me")
    assert masker.redact("remember-me") == MASKED

    masker.reset()

    assert masker.redact("remember-me") == "remember-me"
