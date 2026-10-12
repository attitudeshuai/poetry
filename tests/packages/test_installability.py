from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from poetry.core.constraints.version import Version
from poetry.core.constraints.version import parse_constraint
from poetry.core.packages.dependency import Dependency
from poetry.core.packages.project_package import ProjectPackage
from poetry.core.packages.utils.utils import get_python_constraint_from_marker
from poetry.core.version.markers import parse_marker

from poetry.packages.installability import GridPoint
from poetry.packages.installability import InstallabilityVerifier
from poetry.packages.installability import PlatformProfile
from poetry.packages.installability import parse_target_platform
from poetry.packages.installability import platform_profiles
from poetry.packages.installability import python_grid_versions
from poetry.packages.locker import Locker


if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from packaging.tags import Tag
    from poetry.core.constraints.version import VersionConstraint


LINUX_X86_64 = PlatformProfile("linux", "Linux", "posix", "x86_64")
WIN_X86_64 = PlatformProfile("win32", "Windows", "nt", "x86_64")
MAC_X86_64 = PlatformProfile("darwin", "Darwin", "posix", "x86_64")


@pytest.fixture
def locker_factory(tmp_path: Path) -> Callable[[str], Locker]:
    def _factory(lock: str) -> Locker:
        lock_path = tmp_path / "poetry.lock"
        lock_path.write_text(lock, encoding="utf-8")
        return Locker(lock_path, {})

    return _factory


@pytest.fixture
def root_package_factory() -> Callable[..., ProjectPackage]:
    def _factory(
        *dependencies: str, python_versions: str = ">=3.9,<4.0"
    ) -> ProjectPackage:
        root = ProjectPackage("myproj", "0.1.0")
        root.python_versions = python_versions
        for requirement in dependencies:
            root.add_dependency(Dependency.create_from_pep_508(requirement))
        return root

    return _factory


def _marker_constraints(*markers: str) -> list[VersionConstraint]:
    return [get_python_constraint_from_marker(parse_marker(m)) for m in markers]


def test_python_grid_versions_boundary() -> None:
    declared = parse_constraint(">=3.9,<4.0")
    markers = _marker_constraints('python_version < "3.11"')
    assert python_grid_versions(declared, markers, full=False) == [
        Version.parse("3.9"),
        Version.parse("3.10.0"),
        Version.parse("3.11.0"),
        Version.parse("3.13.0"),
    ]


def test_python_grid_versions_boundary_patch_level() -> None:
    declared = parse_constraint(">=3.10,<3.12")
    markers = _marker_constraints('python_full_version < "3.10.2"')
    versions = python_grid_versions(declared, markers, full=False)
    assert Version.parse("3.10.1") in versions
    assert Version.parse("3.10.2") in versions
    # interior minors that are not transition points are not sampled
    assert Version.parse("3.10.5") not in versions


def test_python_grid_versions_full() -> None:
    declared = parse_constraint(">=3.9,<3.12")
    versions = python_grid_versions(declared, [], full=True)
    assert versions == [
        Version.parse("3.9"),
        Version.parse("3.10.0"),
        Version.parse("3.11.0"),
    ]


def test_python_grid_versions_union() -> None:
    declared = parse_constraint(">=3.9,<3.10 || >=3.11,<4.0")
    versions = python_grid_versions(declared, [], full=True)
    assert Version.parse("3.9") in versions
    assert Version.parse("3.10.0") not in versions
    assert Version.parse("3.11.0") in versions


def test_python_grid_versions_unbounded_uses_running_python() -> None:
    import sys

    versions = python_grid_versions(parse_constraint("*"), [], full=False)
    assert versions == [
        Version.from_parts(
            sys.version_info.major, sys.version_info.minor, sys.version_info.micro
        )
    ]


def test_platform_profiles_default() -> None:
    assert platform_profiles([]) == [MAC_X86_64, LINUX_X86_64, WIN_X86_64]


def test_platform_profiles_from_markers() -> None:
    markers = [
        parse_marker('sys_platform == "linux"'),
        parse_marker('platform_machine == "aarch64"'),
    ]
    profiles = platform_profiles(markers)
    assert PlatformProfile("linux", "Linux", "posix", "aarch64") in profiles
    # x86_64 baseline is always retained
    assert LINUX_X86_64 in profiles
    # windows/darwin not referenced and not part of the default set
    assert WIN_X86_64 not in profiles
    assert MAC_X86_64 not in profiles


def test_parse_target_platform_aliases() -> None:
    assert parse_target_platform("win32/amd64") == WIN_X86_64
    assert parse_target_platform("darwin/arm64") == PlatformProfile(
        "darwin", "Darwin", "posix", "arm64"
    )
    assert parse_target_platform("linux/arm64").machine == "aarch64"


@pytest.mark.parametrize("spec", ["", "foo/x86_64", "linux/foo", "linux"])
def test_parse_target_platform_invalid(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_target_platform(spec)


def test_wheel_tag_supported() -> None:
    point = GridPoint(Version.parse("3.11.0"), LINUX_X86_64)
    assert point.wheel_tag_supported(_tag("foo-1.0-py3-none-any.whl"))
    assert point.wheel_tag_supported(
        _tag("foo-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")
    )
    # forward compatible stable abi
    assert point.wheel_tag_supported(
        _tag("foo-1.0-cp39-abi3-manylinux_2_17_x86_64.whl")
    )
    # older interpreter specific wheel
    assert not point.wheel_tag_supported(
        _tag("foo-1.0-cp310-cp310-manylinux_2_17_x86_64.whl")
    )
    # foreign platform
    assert not point.wheel_tag_supported(
        _tag("foo-1.0-cp311-cp311-win_amd64.whl")
    )

    win_point = GridPoint(Version.parse("3.11.0"), WIN_X86_64)
    assert win_point.wheel_tag_supported(
        _tag("foo-1.0-cp311-cp311-win_amd64.whl")
    )
    assert not win_point.wheel_tag_supported(
        _tag("foo-1.0-cp311-cp311-manylinux_2_17_x86_64.whl")
    )


def _tag(filename: str) -> Tag:
    from packaging.utils import parse_wheel_filename

    return next(iter(parse_wheel_filename(filename)[3]))


HEALTHY_LOCK = """\
[[package]]
name = "demo"
version = "1.0.0"
description = ""
optional = false
python-versions = "*"
groups = ["main"]
files = [{file = "demo-1.0.0-py3-none-any.whl", hash = "sha256:0"}]

[metadata]
lock-version = "2.1"
python-versions = ">=3.9,<4.0"
content-hash = "deadbeef"
"""


def test_verifier_healthy(locker_factory: Callable[[str], Locker]) -> None:
    report = InstallabilityVerifier(locker_factory(HEALTHY_LOCK)).verify()
    assert report.errors == []
    assert report.warnings == []
    # boundary of >=3.9,<4.0 times three default platforms
    assert report.checked_points == 2 * 3


def test_verifier_platform_hole(locker_factory: Callable[[str], Locker]) -> None:
    # lib 1.0 ships an sdist for old pythons; lib 2.0 ships linux wheels only
    lock = """\
[[package]]
name = "demo"
version = "1.0.0"
description = ""
optional = false
python-versions = "*"
groups = ["main"]
files = [{file = "demo-1.0.0.tar.gz", hash = "sha256:0"}]

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
files = [{file = "lib-1.0.0.tar.gz", hash = "sha256:a"}]

[[package]]
name = "lib"
version = "2.0.0"
description = ""
optional = false
python-versions = ">=3.11"
groups = ["main"]
markers = 'python_version >= "3.11"'
files = [
  {file = "lib-2.0.0-cp311-cp311-manylinux_2_17_x86_64.whl", hash = "sha256:a"},
  {file = "lib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl", hash = "sha256:c"},
]

[metadata]
lock-version = "2.1"
python-versions = ">=3.9,<4.0"
content-hash = "deadbeef"
"""
    report = InstallabilityVerifier(locker_factory(lock)).verify()
    assert len(report.errors) == 1
    message = report.errors[0]
    assert "lib" in message
    assert "py3.11-win32-x86_64" in message
    assert "py3.11-darwin-x86_64" in message
    assert "py3.13-win32-x86_64" in message
    assert "manylinux" in message
    # linux points are installable and must not be reported
    assert "py3.11-linux" not in message
    assert "py3.9" not in message


def test_verifier_full_grid_finds_interior_gap(
    locker_factory: Callable[[str], Locker],
    root_package_factory: Callable[..., ProjectPackage],
) -> None:
    """The cp312 wheel is missing: boundary sampling (range endpoints and the
    3.11 marker transition) misses it, the full grid samples every minor and
    finds it."""
    lock = """\
[[package]]
name = "demo"
version = "1.0.0"
description = ""
optional = false
python-versions = "*"
groups = ["main"]
files = [{file = "demo-1.0.0.tar.gz", hash = "sha256:0"}]

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
files = [{file = "lib-1.0.0.tar.gz", hash = "sha256:a"}]

[[package]]
name = "lib"
version = "2.0.0"
description = ""
optional = false
python-versions = ">=3.11"
groups = ["main"]
markers = 'python_version >= "3.11"'
files = [
  {file = "lib-2.0.0-cp311-cp311-manylinux_2_17_x86_64.whl", hash = "sha256:b"},
  {file = "lib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl", hash = "sha256:c"},
]

[metadata]
lock-version = "2.1"
python-versions = ">=3.9,<3.14"
content-hash = "deadbeef"
"""
    root = root_package_factory("demo", python_versions=">=3.9,<3.14")
    verifier = InstallabilityVerifier(locker_factory(lock), root)
    assert verifier.verify(mode="boundary", target_platforms=["linux/x86_64"]).errors == []
    full = verifier.verify(mode="full", target_platforms=["linux/x86_64"])
    assert any("py3.12-linux-x86_64" in error for error in full.errors)


def test_verifier_contradictory_markers(locker_factory: Callable[[str], Locker]) -> None:
    lock = """\
[[package]]
name = "demo"
version = "1.0.0"
description = ""
optional = false
python-versions = ">=3.10"
groups = ["main"]
markers = 'python_version < "3.10" and python_version >= "3.10"'
files = [{file = "demo-1.0.0.tar.gz", hash = "sha256:0"}]

[metadata]
lock-version = "2.1"
python-versions = ">=3.9,<4.0"
content-hash = "deadbeef"
"""
    report = InstallabilityVerifier(locker_factory(lock)).verify()
    assert any("contradictory package markers" in w for w in report.warnings)
    assert any("not enabled at any" in w for w in report.warnings)


def test_verifier_dead_dependency_entry(locker_factory: Callable[[str], Locker]) -> None:
    """A win32 requirement of a package that can only exist on linux/darwin
    is a dead alternative."""
    lock = """\
[[package]]
name = "demo"
version = "1.0.0"
description = ""
optional = false
python-versions = "*"
groups = ["main"]
markers = 'sys_platform == "linux" or sys_platform == "darwin"'
files = [{file = "demo-1.0.0.tar.gz", hash = "sha256:0"}]

[package.dependencies]
pywin32-ctypes = {version = "*", markers = 'sys_platform == "win32"'}

[[package]]
name = "pywin32-ctypes"
version = "0.2.0"
description = ""
optional = false
python-versions = "*"
groups = ["main"]
markers = 'sys_platform == "win32"'
files = [{file = "pywin32_ctypes-0.2.0-py3-none-any.whl", hash = "sha256:1"}]

[metadata]
lock-version = "2.1"
python-versions = ">=3.9,<4.0"
content-hash = "deadbeef"
"""
    report = InstallabilityVerifier(locker_factory(lock)).verify()
    assert any("is dead and can never be installed" in w for w in report.warnings)
    assert report.errors == []


def test_verifier_source_packages_warning(locker_factory: Callable[[str], Locker]) -> None:
    lock = """\
[[package]]
name = "demo"
version = "1.0.0"
description = ""
optional = false
python-versions = "*"
groups = ["main"]
files = []
source = {type = "git", url = "https://example.com/demo.git"}

[metadata]
lock-version = "2.1"
python-versions = ">=3.9,<4.0"
content-hash = "deadbeef"
"""
    report = InstallabilityVerifier(locker_factory(lock)).verify()
    assert report.errors == []
    assert any("source packages cannot be checked" in w for w in report.warnings)


def test_verifier_target_platform_override(
    locker_factory: Callable[[str], Locker],
    root_package_factory: Callable[..., ProjectPackage],
) -> None:
    # linux-only wheels: targeting linux is clean, targeting windows blocks
    lock = """\
[[package]]
name = "demo"
version = "1.0.0"
description = ""
optional = false
python-versions = "*"
groups = ["main"]
files = [
  {file = "demo-1.0.0-cp39-cp39-manylinux_2_17_x86_64.whl", hash = "sha256:0"},
  {file = "demo-1.0.0-cp310-cp310-manylinux_2_17_x86_64.whl", hash = "sha256:1"},
  {file = "demo-1.0.0-cp311-cp311-manylinux_2_17_x86_64.whl", hash = "sha256:2"},
  {file = "demo-1.0.0-cp312-cp312-manylinux_2_17_x86_64.whl", hash = "sha256:3"},
  {file = "demo-1.0.0-cp313-cp313-manylinux_2_17_x86_64.whl", hash = "sha256:4"},
]

[metadata]
lock-version = "2.1"
python-versions = ">=3.9,<4.0"
content-hash = "deadbeef"
"""
    root = root_package_factory("demo")
    verifier = InstallabilityVerifier(locker_factory(lock), root)
    linux_only = verifier.verify(target_platforms=["linux/x86_64"])
    assert linux_only.errors == []
    assert linux_only.grid_points[0].platform == LINUX_X86_64
    assert len({p.platform for p in linux_only.grid_points}) == 1

    windows = verifier.verify(target_platforms=["win32/amd64"])
    assert windows.errors != []


def test_verifier_does_not_modify_lock(locker_factory: Callable[[str], Locker], tmp_path: Path) -> None:
    lock_path = tmp_path / "poetry.lock"
    lock_path.write_text(HEALTHY_LOCK, encoding="utf-8")
    before = lock_path.read_bytes()
    InstallabilityVerifier(Locker(lock_path, {})).verify(mode="full")
    assert lock_path.read_bytes() == before


def test_verifier_invalid_mode(locker_factory: Callable[[str], Locker]) -> None:
    with pytest.raises(ValueError):
        InstallabilityVerifier(locker_factory(HEALTHY_LOCK)).verify(mode="nope")


def test_verifier_legacy_lock_without_distribution_metadata(
    locker_factory: Callable[[str], Locker],
) -> None:
    lock = """\
[[package]]
name = "demo"
version = "1.0.0"
description = ""
optional = false
python-versions = "*"

[metadata]
lock-version = "2.0"
python-versions = "^3.8"
content-hash = "abc"
"""
    with pytest.raises(ValueError, match="Regenerate it"):
        InstallabilityVerifier(locker_factory(lock)).verify()

