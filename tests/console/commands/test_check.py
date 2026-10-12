from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from poetry.factory import Factory
from poetry.packages import Locker
from poetry.toml import TOMLFile
from poetry.utils._compat import tomllib


if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from cleo.testers.command_tester import CommandTester
    from pytest_mock import MockerFixture

    from poetry.poetry import Poetry
    from tests.types import CommandTesterFactory
    from tests.types import FixtureDirGetter
    from tests.types import SetProjectContext


INSTALLABILITY_PYPROJECT = """\
[project]
name = "myproj"
version = "0.1.0"
requires-python = ">=3.9,<4.0"
dependencies = ["demo"]
"""


def _installability_lock() -> str:
    return """\
[[package]]
name = "demo"
version = "1.0.0"
description = ""
optional = false
python-versions = "*"
groups = ["main"]
files = [{file = "demo-1.0.0.tar.gz", hash = "sha256:0000"}]

[package.dependencies]
lib = "*"

[[package]]
name = "lib"
version = "1.0.0"
description = ""
optional = false
python-versions = ">=3.9"
groups = ["main"]
markers = 'python_version < "3.11"'
files = [{file = "lib-1.0.0.tar.gz", hash = "sha256:aaaa"}]

[[package]]
name = "lib"
version = "2.0.0"
description = ""
optional = false
python-versions = ">=3.11"
groups = ["main"]
markers = 'python_version >= "3.11"'
files = [
  {file = "lib-2.0.0-cp311-cp311-manylinux_2_17_x86_64.whl", hash = "sha256:bbbb"},
  {file = "lib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl", hash = "sha256:cccc"},
]

[metadata]
lock-version = "2.1"
python-versions = ">=3.9,<4.0"
content-hash = "__HASH__"
"""


@pytest.fixture
def installability_poetry(tmp_path: Path) -> Poetry:
    (tmp_path / "pyproject.toml").write_text(
        INSTALLABILITY_PYPROJECT, encoding="utf-8"
    )
    with (tmp_path / "pyproject.toml").open("rb") as f:
        pyproject_data = tomllib.load(f)
    content_hash = Locker(
        tmp_path / "poetry.lock", pyproject_data
    )._get_content_hash()
    (tmp_path / "poetry.lock").write_text(
        _installability_lock().replace("__HASH__", content_hash),
        encoding="utf-8",
    )
    return Factory().create_poetry(tmp_path)


@pytest.fixture
def poetry_simple_project(set_project_context: SetProjectContext) -> Iterator[Poetry]:
    with set_project_context("simple_project", in_place=False) as cwd:
        yield Factory().create_poetry(cwd)


@pytest.fixture
def poetry_with_outdated_lockfile(
    set_project_context: SetProjectContext,
) -> Iterator[Poetry]:
    with set_project_context("outdated_lock", in_place=False) as cwd:
        yield Factory().create_poetry(cwd)


@pytest.fixture
def poetry_with_up_to_date_lockfile(
    set_project_context: SetProjectContext,
) -> Iterator[Poetry]:
    with set_project_context("up_to_date_lock", in_place=False) as cwd:
        yield Factory().create_poetry(cwd)


@pytest.fixture
def poetry_with_pypi_reference(
    set_project_context: SetProjectContext,
) -> Iterator[Poetry]:
    with set_project_context("pypi_reference", in_place=False) as cwd:
        yield Factory().create_poetry(cwd)


@pytest.fixture
def poetry_with_invalid_pyproject(
    set_project_context: SetProjectContext,
) -> Iterator[Poetry]:
    with set_project_context("invalid_pyproject", in_place=False) as cwd:
        yield Factory().create_poetry(cwd)


@pytest.fixture()
def tester(
    command_tester_factory: CommandTesterFactory, poetry_simple_project: Poetry
) -> CommandTester:
    return command_tester_factory("check", poetry=poetry_simple_project)


def test_check_valid(tester: CommandTester) -> None:
    tester.execute()

    expected = """\
All set!
"""

    assert tester.io.fetch_output() == expected


@pytest.mark.parametrize(
    ["args", "expected_status"],
    [
        ([], 0),
        (["--strict"], 1),
    ],
)
def test_check_valid_legacy(
    args: list[str],
    expected_status: int,
    mocker: MockerFixture,
    tester: CommandTester,
    fixture_dir: FixtureDirGetter,
) -> None:
    mocker.patch(
        "poetry.poetry.Poetry.file",
        return_value=TOMLFile(fixture_dir("simple_project_legacy") / "pyproject.toml"),
        new_callable=mocker.PropertyMock,
    )
    tester.execute(" ".join(args))

    expected = (
        "Warning: [tool.poetry.name] is deprecated. Use [project.name] instead.\n"
        "Warning: [tool.poetry.version] is set but 'version' is not in "
        "[project.dynamic]. If it is static use [project.version]. If it is dynamic, "
        "add 'version' to [project.dynamic].\n"
        "If you want to set the version dynamically via `poetry build "
        "--local-version` or you are using a plugin, which sets the version "
        "dynamically, you should define the version in [tool.poetry] and add "
        "'version' to [project.dynamic].\n"
        "Warning: [tool.poetry.description] is deprecated. Use [project.description] "
        "instead.\n"
        "Warning: [tool.poetry.readme] is set but 'readme' is not in "
        "[project.dynamic]. If it is static use [project.readme]. If it is dynamic, "
        "add 'readme' to [project.dynamic].\n"
        "If you want to define multiple readmes, you should define them in "
        "[tool.poetry] and add 'readme' to [project.dynamic].\n"
        "Warning: [tool.poetry.license] is deprecated. Use [project.license] instead.\n"
        "Warning: [tool.poetry.authors] is deprecated. Use [project.authors] instead.\n"
        "Warning: [tool.poetry.keywords] is deprecated. Use [project.keywords] "
        "instead.\n"
        "Warning: [tool.poetry.classifiers] is set but 'classifiers' is not in "
        "[project.dynamic]. If it is static use [project.classifiers]. If it is "
        "dynamic, add 'classifiers' to [project.dynamic].\n"
        "ATTENTION: Per default Poetry determines classifiers for supported Python "
        "versions and license automatically. If you define classifiers in [project], "
        "you disable the automatic enrichment. In other words, you have to define all "
        "classifiers manually. If you want to use Poetry's automatic enrichment of "
        "classifiers, you should define them in [tool.poetry] and add 'classifiers' "
        "to [project.dynamic].\n"
        "Warning: [tool.poetry.homepage] is deprecated. Use [project.urls] instead.\n"
        "Warning: [tool.poetry.repository] is deprecated. Use [project.urls] instead.\n"
        "Warning: [tool.poetry.documentation] is deprecated. Use [project.urls] "
        "instead.\n"
        "Warning: Defining console scripts in [tool.poetry.scripts] is deprecated. "
        "Use [project.scripts] instead. ([tool.poetry.scripts] should only be used "
        "for scripts of type 'file').\n"
    )

    assert tester.io.fetch_error() == expected
    assert tester.status_code == expected_status


def test_check_invalid_dep_name_same_as_project_name(
    mocker: MockerFixture, tester: CommandTester, fixture_dir: FixtureDirGetter
) -> None:
    mocker.patch(
        "poetry.poetry.Poetry.file",
        return_value=TOMLFile(
            fixture_dir("invalid_pyproject_dep_name") / "pyproject.toml"
        ),
        new_callable=mocker.PropertyMock,
    )
    tester.execute("")

    expected = """\
Error: Project name (invalid) is same as one of its dependencies
"""

    assert tester.io.fetch_error() == expected


def test_check_invalid(
    tester: CommandTester,
    fixture_dir: FixtureDirGetter,
    command_tester_factory: CommandTesterFactory,
    poetry_with_invalid_pyproject: Poetry,
) -> None:
    tester = command_tester_factory("check", poetry=poetry_with_invalid_pyproject)
    tester.execute("--lock")

    expected = """\
Error: Unrecognized classifiers: ['Intended Audience :: Clowns'].
Error: Declared README file does not exist: never/exists.md
Error: Invalid source "not-exists" referenced in dependencies.
Error: Invalid source "not-exists2" referenced in dependencies.
Error: poetry.lock was not found.
Warning: [project.license] is not a valid SPDX expression.\
 This is deprecated and will raise an error in the future.
Warning: A wildcard Python dependency is ambiguous.\
 Consider specifying a more explicit one.
Warning: The "pendulum" dependency specifies the "allows-prereleases" property,\
 which is deprecated. Use "allow-prereleases" instead.
Warning: Deprecated classifier 'Natural Language :: Ukranian'.\
 Must be replaced by ['Natural Language :: Ukrainian'].
Warning: Deprecated classifier\
 'Topic :: Communications :: Chat :: AOL Instant Messenger'.\
 Must be removed.
"""

    assert tester.io.fetch_error() == expected


def test_check_private(
    mocker: MockerFixture, tester: CommandTester, fixture_dir: FixtureDirGetter
) -> None:
    mocker.patch(
        "poetry.poetry.Poetry.file",
        return_value=TOMLFile(fixture_dir("private_pyproject") / "pyproject.toml"),
        new_callable=mocker.PropertyMock,
    )

    tester.execute()

    expected = """\
All set!
"""

    assert tester.io.fetch_output() == expected


def test_check_non_package_mode(
    mocker: MockerFixture, tester: CommandTester, fixture_dir: FixtureDirGetter
) -> None:
    mocker.patch(
        "poetry.poetry.Poetry.file",
        return_value=TOMLFile(fixture_dir("non_package_mode") / "pyproject.toml"),
        new_callable=mocker.PropertyMock,
    )

    tester.execute()

    expected = """\
All set!
"""

    assert tester.io.fetch_output() == expected


@pytest.mark.parametrize(
    ("options", "expected", "expected_status"),
    [
        ("", "All set!\n", 0),
        ("--lock", "Error: poetry.lock was not found.\n", 1),
    ],
)
def test_check_lock_missing(
    mocker: MockerFixture,
    tester: CommandTester,
    fixture_dir: FixtureDirGetter,
    options: str,
    expected: str,
    expected_status: int,
) -> None:
    mocker.patch(
        "poetry.poetry.Poetry.file",
        return_value=TOMLFile(fixture_dir("private_pyproject") / "pyproject.toml"),
        new_callable=mocker.PropertyMock,
    )

    status_code = tester.execute(options)

    assert status_code == expected_status

    if status_code == 0:
        assert tester.io.fetch_output() == expected
    else:
        assert tester.io.fetch_error() == expected


@pytest.mark.parametrize("options", ["", "--lock"])
def test_check_lock_outdated(
    command_tester_factory: CommandTesterFactory,
    poetry_with_outdated_lockfile: Poetry,
    options: str,
) -> None:
    locker = Locker(
        lock=poetry_with_outdated_lockfile.pyproject.file.path.parent / "poetry.lock",
        pyproject_data=poetry_with_outdated_lockfile.locker._pyproject_data,
    )
    poetry_with_outdated_lockfile.set_locker(locker)

    tester = command_tester_factory("check", poetry=poetry_with_outdated_lockfile)
    status_code = tester.execute(options)
    expected = (
        "Error: pyproject.toml changed significantly since poetry.lock was last generated. "
        "Run `poetry lock` to fix the lock file.\n"
    )

    assert tester.io.fetch_error() == expected

    # exit with an error
    assert status_code == 1


@pytest.mark.parametrize("options", ["", "--lock"])
def test_check_lock_up_to_date(
    command_tester_factory: CommandTesterFactory,
    poetry_with_up_to_date_lockfile: Poetry,
    options: str,
) -> None:
    locker = Locker(
        lock=poetry_with_up_to_date_lockfile.pyproject.file.path.parent / "poetry.lock",
        pyproject_data=poetry_with_up_to_date_lockfile.locker._pyproject_data,
    )
    poetry_with_up_to_date_lockfile.set_locker(locker)

    tester = command_tester_factory("check", poetry=poetry_with_up_to_date_lockfile)
    status_code = tester.execute(options)
    expected = "All set!\n"
    assert tester.io.fetch_output() == expected

    # exit with an error
    assert status_code == 0


def test_check_does_not_error_on_pypi_reference(
    command_tester_factory: CommandTesterFactory,
    poetry_with_pypi_reference: Poetry,
) -> None:
    tester = command_tester_factory("check", poetry=poetry_with_pypi_reference)
    status_code = tester.execute("")

    assert tester.io.fetch_output() == "All set!\n"
    assert status_code == 0


@pytest.fixture(params=["project_str", "project_dict", "poetry_str", "poetry_array"])
def pyproject_with_readme_file(tmp_path: Path, request: pytest.FixtureRequest) -> Path:
    pyproject_content = """\
[project]
name = "test"
version = "1.0.0"
"""
    if request.param == "project_str":
        pyproject_content += 'readme = "README.md"\n'

    elif request.param == "project_dict":
        pyproject_content += (
            'readme = { file = "README.md", content-type = "text/markdown" }\n'
        )
    elif request.param == "poetry_str":
        pyproject_content += """
[tool.poetry]
readme = "README.md"
"""
    elif request.param == "poetry_array":
        pyproject_content += """
[tool.poetry]
readme = ["README.md"]
"""
    else:
        raise ValueError(f"Unknown readme type: {request.param}")
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(pyproject_content, encoding="utf-8")
    return pyproject_path


@pytest.mark.parametrize("readme_exists", [True, False])
def test_check_readme_file_exists(
    mocker: MockerFixture,
    tester: CommandTester,
    fixture_dir: FixtureDirGetter,
    tmp_path: Path,
    pyproject_with_readme_file: Path,
    readme_exists: bool,
) -> None:
    if readme_exists:
        readme_path = tmp_path / "README.md"
        readme_path.write_text("README", encoding="utf-8")

    mocker.patch(
        "poetry.poetry.Poetry.file",
        return_value=TOMLFile(pyproject_with_readme_file),
        new_callable=mocker.PropertyMock,
    )
    result = tester.execute()

    if readme_exists:
        assert result == 0
        assert "Declared README file does not exist" not in tester.io.fetch_error()
    else:
        assert result == 1
        assert (
            "Declared README file does not exist: README.md" in tester.io.fetch_error()
        )


def test_check_installability_blocking(
    command_tester_factory: CommandTesterFactory,
    installability_poetry: Poetry,
) -> None:
    tester = command_tester_factory("check", poetry=installability_poetry)
    status_code = tester.execute("--installability")

    error_output = tester.io.fetch_error()
    assert status_code == 1
    assert "No installable candidate for dependency" in error_output
    assert '"lib"' in error_output
    assert "py3.11-win32-x86_64" in error_output
    assert "py3.11-darwin-x86_64" in error_output
    # the exact dependency and marker group is locatable
    assert 'required by "demo 1.0.0" in group main' in error_output
    # linux points are installable
    assert "py3.11-linux" not in error_output


def test_check_installability_full_grid_option(
    command_tester_factory: CommandTesterFactory,
    installability_poetry: Poetry,
) -> None:
    tester = command_tester_factory("check", poetry=installability_poetry)
    # boundary sampling does not look at cp312, full grid does and the
    # cp312 wheel is missing from lib 2.0.0
    status_code = tester.execute("--installability-grid=full")
    error_output = tester.io.fetch_error()
    assert status_code == 1
    assert "py3.12-win32-x86_64" in error_output


def test_check_installability_target_platform_passes(
    command_tester_factory: CommandTesterFactory,
    installability_poetry: Poetry,
) -> None:
    tester = command_tester_factory("check", poetry=installability_poetry)
    # limit the grid to linux, where every version has a wheel or sdist
    status_code = tester.execute("--installability --target-platform=linux/x86_64")
    assert status_code == 0
    assert tester.io.fetch_output() == "All set!\n"


def test_check_installability_default_off_unchanged(
    command_tester_factory: CommandTesterFactory,
    installability_poetry: Poetry,
) -> None:
    tester = command_tester_factory("check", poetry=installability_poetry)
    # without the new options, existing behavior is preserved verbatim
    status_code = tester.execute("")
    assert status_code == 0
    assert tester.io.fetch_output() == "All set!\n"
    assert tester.io.fetch_error() == ""


def test_check_installability_invalid_grid(
    command_tester_factory: CommandTesterFactory,
    installability_poetry: Poetry,
) -> None:
    tester = command_tester_factory("check", poetry=installability_poetry)
    status_code = tester.execute("--installability-grid=bogus")
    assert status_code == 1
    assert 'Invalid value "bogus" for --installability-grid' in (
        tester.io.fetch_error()
    )


def test_check_target_platform_requires_installability(
    command_tester_factory: CommandTesterFactory,
    installability_poetry: Poetry,
) -> None:
    tester = command_tester_factory("check", poetry=installability_poetry)
    status_code = tester.execute("--target-platform=linux/x86_64")
    assert status_code == 1
    assert "--target-platform can only be used together with --installability" in (
        tester.io.fetch_error()
    )


def test_check_installability_does_not_modify_lock(
    command_tester_factory: CommandTesterFactory,
    installability_poetry: Poetry,
) -> None:
    lock_path = installability_poetry.locker.lock
    before = lock_path.read_bytes()
    tester = command_tester_factory("check", poetry=installability_poetry)
    tester.execute("--installability-grid=full")
    assert lock_path.read_bytes() == before


def test_check_installability_strict_fails_on_hints(
    command_tester_factory: CommandTesterFactory,
    tmp_path: Path,
) -> None:
    # demo is active everywhere but declares a dependency that can never be
    # enabled (python_version < "3.9" while the project requires >= 3.9):
    # this is a hint, not a blocking error
    (tmp_path / "pyproject.toml").write_text(
        INSTALLABILITY_PYPROJECT, encoding="utf-8"
    )
    with (tmp_path / "pyproject.toml").open("rb") as f:
        pyproject_data = tomllib.load(f)
    content_hash = Locker(
        tmp_path / "poetry.lock", pyproject_data
    )._get_content_hash()
    lock = """\
[[package]]
name = "demo"
version = "1.0.0"
description = ""
optional = false
python-versions = "*"
groups = ["main"]
files = [{file = "demo-1.0.0.tar.gz", hash = "sha256:0000"}]

[package.dependencies]
oldlib = {version = "*", markers = 'python_version < "3.9"'}

[[package]]
name = "oldlib"
version = "1.0.0"
description = ""
optional = false
python-versions = "*"
groups = ["main"]
markers = 'python_version < "3.9"'
files = [{file = "oldlib-1.0.0.tar.gz", hash = "sha256:dddd"}]

[metadata]
lock-version = "2.1"
python-versions = ">=3.9,<4.0"
content-hash = "__HASH__"
""".replace("__HASH__", content_hash)
    (tmp_path / "poetry.lock").write_text(lock, encoding="utf-8")
    poetry = Factory().create_poetry(tmp_path)

    tester = command_tester_factory("check", poetry=poetry)
    assert tester.execute("--installability") == 0
    assert "is dead and can never be installed" in tester.io.fetch_error()

    tester = command_tester_factory("check", poetry=poetry)
    assert tester.execute("--installability --strict") == 1


@pytest.fixture(params=["project_str", "project_dict", "poetry_str", "poetry_array"])
def pyproject_with_empty_readme_file(
    tmp_path: Path, request: pytest.FixtureRequest
) -> Path:
    pyproject_content = """\
[project]
name = "test"
version = "1.0.0"
"""
    if request.param == "project_str":
        pyproject_content += 'readme = ""\n'

    elif request.param == "project_dict":
        pyproject_content += 'readme = { file = "", content-type = "text/markdown" }\n'
    elif request.param == "poetry_str":
        pyproject_content += """
[tool.poetry]
readme = ""
"""
    elif request.param == "poetry_array":
        pyproject_content += """
[tool.poetry]
readme = [""]
"""
    else:
        raise ValueError(f"Unknown readme type: {request.param}")
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(pyproject_content, encoding="utf-8")
    return pyproject_path


def test_check_project_readme_as_text(
    mocker: MockerFixture,
    tester: CommandTester,
    fixture_dir: FixtureDirGetter,
    tmp_path: Path,
) -> None:
    pyproject_content = """[project]
name = "test"
version = "1.0.0"
readme = { content-type = "text/markdown", text = "README" }
"""
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(pyproject_content, encoding="utf-8")

    mocker.patch(
        "poetry.poetry.Poetry.file",
        return_value=TOMLFile(pyproject_path),
        new_callable=mocker.PropertyMock,
    )
    result = tester.execute()

    assert result == 0
    assert "Declared README file does not exist" not in tester.io.fetch_error()


def test_check_poetry_readme_multiple(
    mocker: MockerFixture,
    tester: CommandTester,
    fixture_dir: FixtureDirGetter,
    tmp_path: Path,
) -> None:
    pyproject_content = """[project]
name = "test"
version = "1.0.0"
dynamic = ["readme"]

[tool.poetry]
readme = ["README1.md", "README2.md", "README3.md", "README4.md"]
"""
    pyproject_path = tmp_path / "pyproject.toml"
    pyproject_path.write_text(pyproject_content, encoding="utf-8")
    (tmp_path / "README2.md").write_text("README 2", encoding="utf-8")
    (tmp_path / "README3.md").write_text("README 3", encoding="utf-8")

    mocker.patch(
        "poetry.poetry.Poetry.file",
        return_value=TOMLFile(pyproject_path),
        new_callable=mocker.PropertyMock,
    )
    result = tester.execute()

    assert result == 1
    assert tester.io.fetch_error() == (
        "Error: Declared README file does not exist: README1.md\n"
        "Error: Declared README file does not exist: README4.md\n"
    )
