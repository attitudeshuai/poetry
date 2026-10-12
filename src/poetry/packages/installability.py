from __future__ import annotations

import re

from dataclasses import dataclass
from typing import TYPE_CHECKING
from typing import Any

from packaging.utils import InvalidWheelFilename
from packaging.utils import canonicalize_name
from packaging.utils import parse_wheel_filename
from poetry.core.constraints.version import Version
from poetry.core.constraints.version import VersionRangeConstraint
from poetry.core.constraints.version import VersionUnion
from poetry.core.constraints.version import parse_constraint
from poetry.core.packages.utils.utils import get_python_constraint_from_marker
from poetry.core.version.markers import BaseMarker
from poetry.core.version.markers import MarkerUnion
from poetry.core.version.markers import MultiMarker
from poetry.core.version.markers import SingleMarkerLike
from poetry.core.version.markers import parse_marker


if TYPE_CHECKING:
    from collections.abc import Iterable
    from collections.abc import Iterator

    from packaging.tags import Tag
    from packaging.utils import NormalizedName
    from poetry.core.constraints.version import VersionConstraint
    from poetry.core.packages.dependency import Dependency
    from poetry.core.packages.package import Package

    from poetry.packages.locker import Locker


_PLATFORM_MARKER_NAMES = (
    "sys_platform",
    "platform_system",
    "os_name",
    "platform_machine",
)

#: Operating system profiles used when expanding platform markers. The values
#: are the (sys_platform, platform_system, os_name) triple used in PEP 508
#: marker environments.
_OS_PROFILES: dict[str, tuple[str, str, str]] = {
    "linux": ("linux", "Linux", "posix"),
    "win32": ("win32", "Windows", "nt"),
    "darwin": ("darwin", "Darwin", "posix"),
}

_OS_ALIASES: dict[str, str] = {
    "linux": "linux",
    "manylinux": "linux",
    "musllinux": "linux",
    "win": "win32",
    "win32": "win32",
    "windows": "win32",
    "nt": "win32",
    "macos": "darwin",
    "macosx": "darwin",
    "darwin": "darwin",
    "osx": "darwin",
}

#: Architecture aliases mapped to a canonical architecture name. The platform
#: specific naming (aarch64 vs arm64) is applied by :func:`_normalize_arch`.
_ARCH_ALIASES = {
    "amd64": "x86_64",
    "x86_64": "x86_64",
    "x64": "x86_64",
    "x86": "i686",
    "i386": "i686",
    "i686": "i686",
    "aarch64": "aarch64",
    "arm64": "arm64",
}

_SDIST_SUFFIXES = (".tar.gz", ".zip")


@dataclass(frozen=True)
class PlatformProfile:
    """A target operating system and architecture."""

    sys_platform: str
    platform_system: str
    os_name: str
    machine: str

    def __str__(self) -> str:
        return f"{self.sys_platform}/{self.machine}"

    @property
    def os_key(self) -> str:
        for key, profile in _OS_PROFILES.items():
            if profile[:2] == (self.sys_platform, self.platform_system):
                return key
        return self.sys_platform


@dataclass(frozen=True)
class GridPoint:
    """One point of the (Python version x platform) verification grid."""

    python_version: Version
    platform: PlatformProfile

    def __str__(self) -> str:
        python = f"py{self.python_version.major}.{self.python_version.minor}"
        if self.python_version.patch or self.python_version.is_prerelease():
            python += f".{self.python_version.patch or 0}"
            if pre := self.python_version.pre:
                python += f"{pre.phase}{pre.number}"
        return f"{python}-{self.platform.sys_platform}-{self.platform.machine}"

    @property
    def marker_env(self) -> dict[str, Any]:
        # Every marker variable must be present: poetry-core treats variables
        # that are missing from the environment as satisfied. ``platform_*``
        # release/version values are deliberately omitted: they cannot be
        # modeled by the grid and markers referencing them stay permissive.
        return {
            "python_version": (
                f"{self.python_version.major}.{self.python_version.minor}"
            ),
            "python_full_version": self.python_version.text,
            "implementation_name": "cpython",
            "implementation_version": self.python_version.text,
            "platform_python_implementation": "CPython",
            "sys_platform": self.platform.sys_platform,
            "platform_system": self.platform.platform_system,
            "os_name": self.platform.os_name,
            "platform_machine": self.platform.machine,
            # Extras are never enabled: marker expressions referencing
            # ``extra`` evaluate to false at every grid point.
            "extra": frozenset[str](),
        }

    def wheel_tag_supported(self, tag: Tag) -> bool:
        if not self._platform_tag_supported(tag.platform):
            return False

        minor = self.python_version.minor or 0
        interpreter = tag.interpreter
        abi = tag.abi

        if interpreter in {"py3", f"py3{minor}"} and abi == "none":
            return True
        if interpreter == f"cp3{minor}" and abi in {"none", "abi3", f"cp3{minor}"}:
            return True
        if abi == "abi3":
            match = re.fullmatch(r"cp3(\d+)", interpreter)
            if match and int(match.group(1)) <= minor:
                return True
        return False

    def _platform_tag_supported(self, platform_tag: str) -> bool:
        if platform_tag == "any":
            return True

        os_key = self.platform.os_key
        machine = self.platform.machine

        if os_key == "linux":
            if platform_tag == f"linux_{machine}":
                return True
            return bool(
                re.fullmatch(
                    r"(?:manylinux\d*|manylinux_\d+_\d+|musllinux_\d+_\d+)"
                    rf"_{re.escape(machine)}",
                    platform_tag,
                )
            )
        if os_key == "win32":
            windows_machine = {
                "x86_64": "win_amd64",
                "i686": "win32",
                "aarch64": "win_arm64",
                "arm64": "win_arm64",
            }.get(machine)
            return bool(windows_machine and platform_tag == windows_machine)
        # darwin
        if machine == "x86_64":
            return bool(
                re.fullmatch(r"macosx_[0-9_]+_(?:x86_64|universal2)", platform_tag)
            )
        if machine in {"arm64", "aarch64"}:
            return bool(
                re.fullmatch(r"macosx_[0-9_]+_(?:arm64|universal2)", platform_tag)
            )
        return False


@dataclass
class InstallabilityReport:
    """Result of an installability verification.

    ``errors`` are blocking findings (an enabled dependency has no installable
    candidate), ``warnings`` are non-blocking hints (contradictory markers,
    dead lock entries, unverifiable source packages).
    """

    errors: list[str]
    warnings: list[str]
    grid_points: list[GridPoint]

    @property
    def checked_points(self) -> int:
        return len(self.grid_points)


def parse_target_platform(spec: str) -> PlatformProfile:
    """Parse a target platform specification like ``linux/x86_64``."""
    parts = re.split(r"[/\-_]", spec.strip().lower(), maxsplit=1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise ValueError(
            f'Invalid target platform "{spec}". Expected "<os>/<arch>", e.g.'
            ' "linux/x86_64", "win32/amd64" or "darwin/arm64".'
        )

    os_key = _OS_ALIASES.get(parts[0])
    if os_key is None:
        raise ValueError(
            f'Unknown operating system "{parts[0]}" in target platform'
            f' "{spec}". Supported: linux, win32/windows, darwin/macos.'
        )

    try:
        machine = _normalize_arch(parts[1], os_key)
    except ValueError:
        raise ValueError(
            f'Unknown architecture "{parts[1]}" in target platform "{spec}".'
            ' Supported: x86_64/amd64, i686/x86, aarch64, arm64.'
        ) from None

    sys_platform, platform_system, os_name = _OS_PROFILES[os_key]
    return PlatformProfile(
        sys_platform=sys_platform,
        platform_system=platform_system,
        os_name=os_name,
        machine=machine,
    )


def _normalize_arch(arch: str, os_key: str) -> str:
    canonical = _ARCH_ALIASES.get(arch)
    if canonical is None:
        raise ValueError(f'Unknown architecture "{arch}"')
    if os_key == "linux" and canonical == "arm64":
        return "aarch64"
    if os_key == "darwin" and canonical == "aarch64":
        return "arm64"
    return canonical


@dataclass(frozen=True)
class _LockedEntry:
    """A locked package together with the raw lock metadata needed for
    installability verification."""

    package: Package
    groups: frozenset[NormalizedName]
    markers: dict[NormalizedName, BaseMarker]
    files: tuple[str, ...]


@dataclass(frozen=True)
class _EdgeProvider:
    """Something that declares dependencies: a locked package or the root
    package. ``group`` is ``None`` for lock files that do not track groups."""

    name: str
    version: str
    python_constraint: VersionConstraint
    group_markers: dict[NormalizedName, BaseMarker]
    dependencies_by_group: dict[NormalizedName | None, list[Dependency]]


class InstallabilityVerifier:
    """Verify that a lock file is installable across a grid of environments.

    The verifier is strictly read-only: it never modifies the lock file.
    """

    def __init__(
        self, locker: Locker, root_package: Package | None = None
    ) -> None:
        self._locker = locker
        self._root_package = root_package

    def verify(
        self,
        *,
        mode: str = "boundary",
        target_platforms: list[str] | None = None,
    ) -> InstallabilityReport:
        if mode not in {"boundary", "full"}:
            raise ValueError(
                f'Invalid installability grid mode "{mode}".'
                ' Expected "boundary" or "full".'
            )

        lock_data = self._locker.lock_data
        metadata = lock_data.get("metadata", {})
        declared = parse_constraint(metadata.get("python-versions", "*"))

        try:
            entries = self._read_locked_entries(lock_data)
        except KeyError:
            # Legacy/incomplete lock files do not always contain the
            # distribution metadata required for installability verification.
            raise ValueError(
                "The lock file is missing distribution metadata for locked"
                " packages (legacy or incomplete lock file). Regenerate it"
                " with the `poetry lock` command to run the installability"
                " check."
            ) from None

        all_markers = list(self._collect_markers(entries))
        marker_python_constraints = [
            pc
            for marker in all_markers
            if not (pc := get_python_constraint_from_marker(marker)).is_empty()
        ]

        python_versions = python_grid_versions(
            declared, marker_python_constraints, full=mode == "full"
        )

        if target_platforms is not None:
            platforms = [parse_target_platform(spec) for spec in target_platforms]
        else:
            platforms = platform_profiles(all_markers)

        grid_points = [
            GridPoint(python_version=version, platform=platform)
            for version in python_versions
            for platform in platforms
        ]

        providers = self._build_providers(entries)
        candidates = self._index_candidates(entries)

        errors, dead_dependency_warnings = self._verify_grid(
            providers, candidates, grid_points
        )
        warnings = self._contradiction_warnings(entries)
        warnings.extend(dead_dependency_warnings)
        warnings.extend(self._dead_package_warnings(entries, grid_points))
        warnings.extend(self._source_package_warnings(entries))

        return InstallabilityReport(
            errors=errors,
            warnings=warnings,
            grid_points=grid_points,
        )

    # ------------------------------------------------------------------
    # Lock reading
    # ------------------------------------------------------------------

    def _read_locked_entries(
        self, lock_data: dict[str, Any]
    ) -> list[_LockedEntry]:
        entries: list[_LockedEntry] = []
        for info in lock_data.get("package", []):
            package = self._locker._get_locked_package(info)
            raw_groups = info.get("groups")
            groups: frozenset[NormalizedName]
            markers: dict[NormalizedName, BaseMarker]
            if raw_groups is not None:
                # Package applicability is tracked since lock version 2.1.
                groups = frozenset(canonicalize_name(g) for g in raw_groups)
                raw_markers = info.get("markers", "*")
                if isinstance(raw_markers, str):
                    marker = parse_marker(raw_markers)
                    markers = dict.fromkeys(groups, marker)
                else:
                    markers = {
                        canonicalize_name(group): parse_marker(
                            raw_markers.get(group, "*")
                        )
                        for group in raw_markers
                    }
                    for group in groups:
                        markers.setdefault(group, parse_marker("*"))
            else:
                # lock-version < 2.1 does not track package applicability
                groups = frozenset()
                markers = {}

            files = tuple(
                f["file"] for f in info.get("files", []) if isinstance(f, dict)
            )
            entries.append(
                _LockedEntry(
                    package=package,
                    groups=groups,
                    markers=markers,
                    files=files,
                )
            )
        return entries

    @staticmethod
    def _collect_markers(entries: list[_LockedEntry]) -> Iterator[BaseMarker]:
        seen: set[BaseMarker] = set()
        for entry in entries:
            for marker in entry.markers.values():
                if marker not in seen:
                    seen.add(marker)
                    yield marker
            for dependency in entry.package.requires:
                if dependency.marker not in seen:
                    seen.add(dependency.marker)
                    yield dependency.marker

    @staticmethod
    def _index_candidates(
        entries: list[_LockedEntry],
    ) -> dict[NormalizedName, list[_LockedEntry]]:
        candidates: dict[NormalizedName, list[_LockedEntry]] = {}
        for entry in entries:
            candidates.setdefault(entry.package.name, []).append(entry)
        return candidates

    def _build_providers(
        self, entries: list[_LockedEntry]
    ) -> list[_EdgeProvider]:
        providers: list[_EdgeProvider] = []
        for entry in entries:
            requires = list(entry.package.requires)
            dependencies_by_group: dict[
                NormalizedName | None, list[Dependency]
            ]
            if entry.groups:
                dependencies_by_group = dict.fromkeys(entry.groups, requires)
            else:
                dependencies_by_group = {None: requires}
            providers.append(
                _EdgeProvider(
                    name=entry.package.pretty_name,
                    version=entry.package.pretty_version,
                    python_constraint=entry.package.python_constraint,
                    group_markers=dict(entry.markers),
                    dependencies_by_group=dependencies_by_group,
                )
            )

        root = self._root_package
        if root is not None:
            root_dependencies: dict[
                NormalizedName | None, list[Dependency]
            ] = {}
            for group_name in sorted(root.dependency_group_names()):
                group = root.dependency_group(group_name)
                if group.is_optional():
                    continue
                root_dependencies[canonicalize_name(group_name)] = list(
                    group.dependencies
                )
            if root_dependencies:
                providers.append(
                    _EdgeProvider(
                        name=root.pretty_name,
                        version=root.pretty_version,
                        python_constraint=root.python_constraint,
                        group_markers={},
                        dependencies_by_group=root_dependencies,
                    )
                )
        return providers

    # ------------------------------------------------------------------
    # Static checks
    # ------------------------------------------------------------------

    @staticmethod
    def _contradiction_warnings(entries: list[_LockedEntry]) -> list[str]:
        warnings: list[str] = []
        seen: set[str] = set()

        def emit(message: str) -> None:
            if message not in seen:
                seen.add(message)
                warnings.append(message)

        for entry in entries:
            package_label = (
                f"{entry.package.pretty_name} {entry.package.pretty_version}"
            )
            for group in sorted(entry.markers):
                if entry.markers[group].is_empty():
                    emit(
                        f'Locked package "{package_label}" (group {group}) has'
                        " contradictory package markers that can never match"
                        " any environment."
                    )

            for dependency in entry.package.requires:
                if dependency.marker.is_empty():
                    emit(
                        f'Locked dependency "{dependency.pretty_name}" of'
                        f' "{package_label}" has contradictory markers'
                        f' ("{dependency.marker!s}") and can never be enabled.'
                    )
        return warnings

    @staticmethod
    def _dead_package_warnings(
        entries: list[_LockedEntry], grid_points: list[GridPoint]
    ) -> list[str]:
        if not grid_points:
            return []

        warnings: list[str] = []
        for entry in entries:
            if entry.package.optional or not entry.markers:
                continue
            activated = any(
                entry.package.python_constraint.allows(point.python_version)
                and any(
                    marker.validate(point.marker_env)
                    for marker in entry.markers.values()
                )
                for point in grid_points
            )
            if activated:
                continue
            marker_str = ", ".join(
                f"{group}: {marker!s}" if not marker.is_any() else f"{group}: *"
                for group, marker in sorted(entry.markers.items())
            )
            warnings.append(
                f'Locked package "{entry.package.pretty_name}'
                f' {entry.package.pretty_version}" (markers {marker_str},'
                f" requires-python {entry.package.python_versions}) is not"
                " enabled at any of the checked grid points and cannot be"
                " installed anywhere in the declared Python range."
            )
        return warnings

    @staticmethod
    def _source_package_warnings(entries: list[_LockedEntry]) -> list[str]:
        source_packages = sorted(
            f"{entry.package.pretty_name} {entry.package.pretty_version}"
            for entry in entries
            if not entry.files
            and entry.package.source_type in {"git", "directory", "file", "url"}
        )
        if not source_packages:
            return []
        shown = ", ".join(source_packages[:5])
        if len(source_packages) > 5:
            shown += f", ... ({len(source_packages)} in total)"
        return [
            (
                "Distribution files of source packages cannot be checked from"
                f" lock data; skipped: {shown}."
            )
        ]

    # ------------------------------------------------------------------
    # Grid verification
    # ------------------------------------------------------------------

    def _verify_grid(
        self,
        providers: list[_EdgeProvider],
        candidates: dict[NormalizedName, list[_LockedEntry]],
        grid_points: list[GridPoint],
    ) -> tuple[list[str], list[str]]:
        # failure signature -> affected grid point labels
        failures: dict[tuple[str, ...], set[str]] = {}
        # (provider id, dependency signature, group) enabled at some point
        enabled: set[tuple[int, tuple[str, str, str], str | None]] = set()
        active_providers: set[int] = set()

        for point in grid_points:
            env = point.marker_env
            for provider_id, provider in enumerate(providers):
                if not provider.python_constraint.allows(point.python_version):
                    continue
                for group, dependencies in provider.dependencies_by_group.items():
                    if group is not None:
                        group_marker = provider.group_markers.get(group)
                        if group_marker is not None and not group_marker.validate(
                            env
                        ):
                            continue
                    active_providers.add(provider_id)

                    for dep_list in self._group_dependencies(dependencies).values():
                        active = [
                            dep for dep in dep_list if dep.marker.validate(env)
                        ]
                        for dep in active:
                            enabled.add(
                                (
                                    provider_id,
                                    _dependency_signature(dep),
                                    group,
                                )
                            )
                        if not active:
                            continue

                        satisfied = False
                        reasons: list[str] = []
                        for dep in active:
                            ok, reason = self._candidate_status(
                                dep=dep,
                                group=group,
                                point=point,
                                candidates=candidates,
                            )
                            if ok:
                                satisfied = True
                                break
                            reasons.append(reason)

                        if satisfied:
                            continue

                        marker_str = " or ".join(
                            "*" if dep.marker.is_any() else str(dep.marker)
                            for dep in active
                        )
                        first = active[0]
                        key = (
                            provider.name,
                            provider.version,
                            first.pretty_name,
                            str(first.pretty_constraint),
                            marker_str,
                            group or "-",
                            reasons[0],
                        )
                        failures.setdefault(key, set()).add(str(point))

        errors = [
            self._format_failure(key, labels)
            for key, labels in sorted(
                failures.items(), key=lambda item: (min(item[1]), item[0])
            )
        ]
        dead_dependencies = self._dead_dependency_warnings(
            providers, active_providers, enabled
        )
        return errors, dead_dependencies

    @staticmethod
    def _dead_dependency_warnings(
        providers: list[_EdgeProvider],
        active_providers: set[int],
        enabled: set[tuple[int, tuple[str, str, str], str | None]],
    ) -> list[str]:
        # dependency signature -> groups in which it is declared/enabled
        declared_groups: dict[
            tuple[int, tuple[str, str, str]], set[str | None]
        ] = {}
        for provider_id, provider in enumerate(providers):
            if provider_id not in active_providers:
                # Dead packages are already reported separately.
                continue
            for group, dependencies in provider.dependencies_by_group.items():
                for dep in dependencies:
                    if dep.marker.is_any() or _references_extra(dep.marker):
                        continue
                    signature = _dependency_signature(dep)
                    declared_groups.setdefault(
                        (provider_id, signature), set()
                    ).add(group)

        warnings: list[str] = []
        seen: set[str] = set()
        for (provider_id, signature), groups in sorted(
            declared_groups.items(),
            key=lambda item: (
                providers[item[0][0]].name,
                item[0][1],
            ),
        ):
            dead_groups = {
                group
                for group in groups
                if (provider_id, signature, group) not in enabled
            }
            if dead_groups != groups:
                # still enabled in some other group
                continue
            provider = providers[provider_id]
            dep_name, constraint, marker = signature
            constraint_text = (
                dep_name if constraint == "*" else f"{dep_name} ({constraint})"
            )
            message = (
                f'Locked dependency "{constraint_text}" with marker'
                f' "{marker}" of "{provider.name} {provider.version}" is not'
                " enabled at any checked grid point where the package is"
                " active; the requirement is dead and can never be"
                " installed."
            )
            if message not in seen:
                seen.add(message)
                warnings.append(message)
        return warnings

    @staticmethod
    def _group_dependencies(
        dependencies: list[Dependency],
    ) -> dict[NormalizedName, list[Dependency]]:
        grouped: dict[NormalizedName, list[Dependency]] = {}
        for dependency in dependencies:
            grouped.setdefault(dependency.name, []).append(dependency)
        return grouped

    @staticmethod
    def _candidate_status(
        *,
        dep: Dependency,
        group: NormalizedName | None,
        point: GridPoint,
        candidates: dict[NormalizedName, list[_LockedEntry]],
    ) -> tuple[bool, str]:
        locked = candidates.get(dep.name, [])
        locked_versions = sorted(
            {entry.package.pretty_version for entry in locked}
        )

        if not locked:
            return False, "there is no locked package with this name"

        python_matching = [
            entry
            for entry in locked
            if entry.package.python_constraint.allows(point.python_version)
        ]
        if not python_matching:
            ranges = sorted({entry.package.python_versions for entry in locked})
            return (
                False,
                (
                    "none of the locked versions supports Python"
                    f" {point.python_version.major}.{point.python_version.minor}"
                    f" (locked requires-python {ranges})"
                ),
            )

        if group is not None:
            group_matching = [
                entry
                for entry in python_matching
                if group in entry.markers
                and entry.markers[group].validate(point.marker_env)
            ]
            if not group_matching:
                return (
                    False,
                    (
                        f"no locked version is enabled for group {group} at this"
                        " environment"
                    ),
                )
            applicable = group_matching
        else:
            applicable = python_matching

        constraint_matching = [
            entry
            for entry in applicable
            if dep.constraint.allows(entry.package.version)
        ]
        if not constraint_matching:
            return (
                False,
                (
                    f"locked versions {locked_versions} do not satisfy constraint"
                    f' "{dep.pretty_constraint}"'
                ),
            )

        installable = [
            entry
            for entry in constraint_matching
            if _has_installable_file(entry, point)
        ]
        if not installable:
            details = "; ".join(
                f"{entry.package.pretty_version}: {_describe_files(entry.files)}"
                for entry in constraint_matching
            )
            return (
                False,
                (
                    "no locked distribution is compatible with the target platform"
                    f" ({details})"
                ),
            )

        return True, ""

    @staticmethod
    def _format_failure(
        key: tuple[str, ...], labels: set[str]
    ) -> str:
        (
            parent,
            parent_version,
            dep_name,
            constraint,
            marker_str,
            group,
            reason,
        ) = key
        constraint_text = dep_name if constraint == "*" else f"{dep_name} ({constraint})"
        marker_text = "" if marker_str == "*" else f' with marker "{marker_str}"'
        group_text = "" if group == "-" else f" in group {group}"
        return (
            f'No installable candidate for dependency "{constraint_text}"'
            f"{marker_text} required by"
            f' "{parent} {parent_version}"{group_text} at grid point(s)'
            f" {', '.join(sorted(labels))}: {reason}."
        )


def _has_installable_file(entry: _LockedEntry, point: GridPoint) -> bool:
    """Whether the locked package ships a distribution usable at the grid point.

    A source distribution can be built on any platform; a wheel must expose a
    tag supported by the grid point. Packages without recorded files (source
    packages) are assumed installable and reported separately as warnings.
    """
    if not entry.files:
        return True

    for filename in entry.files:
        lower = filename.lower()
        if lower.endswith(_SDIST_SUFFIXES):
            return True
        if not lower.endswith(".whl"):
            continue
        try:
            _, _, _, tags = parse_wheel_filename(filename)
        except InvalidWheelFilename:
            # An invalid wheel filename cannot be a candidate but must not
            # crash verification of the remaining files.
            continue
        if any(point.wheel_tag_supported(tag) for tag in tags):
            return True
    return False


def _describe_files(files: tuple[str, ...]) -> str:
    if not files:
        return "no distributions recorded in the lock file"
    wheels = [f for f in files if f.lower().endswith(".whl")]
    sdists = [f for f in files if f.lower().endswith(_SDIST_SUFFIXES)]
    others = [
        f
        for f in files
        if not f.lower().endswith(".whl") and not f.lower().endswith(_SDIST_SUFFIXES)
    ]
    parts: list[str] = []
    if wheels:
        parts.append(f"wheels {wheels}")
    if others:
        parts.append(f"other files {others}")
    if not parts:
        parts.append(f"source distributions {sdists}")
    return "; ".join(parts)


# ----------------------------------------------------------------------
# Grid construction
# ----------------------------------------------------------------------


def _running_python() -> tuple[int, int, int]:
    import sys

    return sys.version_info.major, sys.version_info.minor, sys.version_info.micro


def _iter_ranges(
    constraint: VersionConstraint,
) -> Iterator[VersionRangeConstraint]:
    if isinstance(constraint, VersionRangeConstraint):
        # VersionRange is a subclass of VersionRangeConstraint.
        yield constraint
    elif isinstance(constraint, VersionUnion):
        yield from constraint.ranges
    # EmptyConstraint contributes no finite ranges.


def _boundary_neighbors(version: Version) -> tuple[Version, Version]:
    """Return the "at boundary" and "just below boundary" versions.

    A ``dev0`` release is the internal representation of an exclusive minor
    boundary (e.g. ``python_version < "3.11"`` becomes ``< 3.11.dev0``).
    """
    major = version.major
    minor = version.minor or 0
    patch = version.patch or 0
    if version.dev is not None and version.pre is None and patch == 0:
        at = Version.from_parts(major, minor, 0)
        below_minor = minor - 1
        below_major = major
        if below_minor < 0:
            # Exclusive major boundary (e.g. "< 4.0"): the newest minor of
            # the previous major cannot be derived from the constraint; use
            # the running interpreter version as the known representative.
            below_major -= 1
            running_major, running_minor, _ = _running_python()
            below_minor = running_minor if running_major == below_major else 0
        below = Version.from_parts(below_major, below_minor, 0)
        return at, below

    if patch > 0:
        below = Version.from_parts(major, minor, patch - 1)
    else:
        below = Version.from_parts(major, max(minor - 1, 0), 0)
    return version, below


def _boundary_versions(
    constraints: Iterable[VersionConstraint],
) -> set[Version]:
    versions: set[Version] = set()
    for constraint in constraints:
        for version_range in _iter_ranges(constraint):
            for boundary in (version_range.min, version_range.max):
                if boundary is None:
                    continue
                at, below = _boundary_neighbors(boundary)
                versions.add(at)
                versions.add(below)
    return versions


def python_grid_versions(
    declared: VersionConstraint,
    marker_constraints: Iterable[VersionConstraint],
    *,
    full: bool,
) -> list[Version]:
    """Build the Python version dimension of the verification grid.

    In ``boundary`` mode only the endpoints of the declared range and the
    transition points of every Python marker in the lock are sampled. In
    ``full`` mode every minor release inside the declared range is sampled,
    plus the exact patch-level transition points.
    """
    versions = _boundary_versions([declared, *marker_constraints])

    if full:
        versions.update(_all_minor_versions(declared))

    versions = {version for version in versions if declared.allows(version)}

    if not versions:
        # Unbounded ranges without marker transition points are sampled at
        # the running interpreter version.
        versions = {Version.from_parts(*_running_python())}

    return sorted(versions)


def _all_minor_versions(declared: VersionConstraint) -> set[Version]:
    versions: set[Version] = set()
    running_major, running_minor, _ = _running_python()
    for version_range in _iter_ranges(declared):
        if version_range.min is not None:
            start_major = version_range.min.major
            start_minor = version_range.min.minor or 0
            if (
                not version_range.include_min
                and (version_range.min.patch or 0) == 0
            ):
                start_minor += 1
        else:
            start_major, start_minor = running_major, running_minor

        if version_range.max is not None:
            max_version = version_range.max
            end_major = max_version.major
            end_minor = max_version.minor or 0
            if not version_range.include_max and max_version.dev is not None and (
                max_version.patch or 0
            ) == 0:
                # Exclusive minor boundary: the newest minor inside the range
                # is the previous one. A major boundary falls back to the
                # running interpreter's latest known minor.
                end_minor -= 1
                if end_minor < 0:
                    end_major -= 1
                    end_minor = running_minor if end_major == running_major else 0
        else:
            end_major, end_minor = running_major, running_minor

        # Python releases newer than the running interpreter cannot be
        # certified, so clamp the ceiling to the known latest minor.
        end_major, end_minor = min(
            (end_major, end_minor), (running_major, running_minor)
        )

        major, minor = start_major, start_minor
        while (major, minor) <= (end_major, end_minor):
            versions.add(Version.from_parts(major, minor, 0))
            minor += 1
            if minor > 99:  # defensive: no Python release uses that many minors
                major += 1
                minor = 0
    return versions


def _iter_single_markers(marker: BaseMarker) -> Iterator[SingleMarkerLike[Any]]:
    if isinstance(marker, SingleMarkerLike):
        yield marker
    elif isinstance(marker, (MultiMarker, MarkerUnion)):
        for sub_marker in marker.markers:
            yield from _iter_single_markers(sub_marker)


def _references_extra(marker: BaseMarker) -> bool:
    return any(single.name == "extra" for single in _iter_single_markers(marker))


def _dependency_signature(dep: Dependency) -> tuple[str, str, str]:
    return (
        dep.pretty_name,
        str(dep.pretty_constraint),
        "*" if dep.marker.is_any() else str(dep.marker),
    )


def _marker_string_values(marker: BaseMarker, name: str) -> set[str]:
    values: set[str] = set()
    for single in _iter_single_markers(marker):
        if single.name != name:
            continue
        constraint = single.constraint
        raw_value = getattr(constraint, "value", None)
        operator = getattr(single, "operator", None)
        if raw_value is not None and operator in {"==", "in"}:
            values.add(str(raw_value))
        sub_constraints = getattr(constraint, "constraints", None)
        if sub_constraints is not None:
            for sub in sub_constraints:
                if getattr(sub, "operator", None) == "==":
                    values.add(str(sub.value))
    return values


def platform_profiles(markers: Iterable[BaseMarker]) -> list[PlatformProfile]:
    """Derive target platforms from the platform marker expressions used in
    the lock file.

    When no marker references a platform variable, the lock implicitly claims
    universal compatibility, so the three supported operating systems
    (Linux, Windows, macOS) on x86_64 are checked.
    """
    marker_list = list(markers)
    values = {
        name: {
            value
            for marker in marker_list
            for value in _marker_string_values(marker, name)
        }
        for name in _PLATFORM_MARKER_NAMES
    }

    os_keys: set[str] = set()
    for spec in values["sys_platform"] | values["platform_system"]:
        alias = _OS_ALIASES.get(spec.lower())
        if alias:
            os_keys.add(alias)
    if "nt" in values["os_name"]:
        os_keys.add("win32")
    if "posix" in values["os_name"] and not os_keys:
        # ``posix`` alone is ambiguous between Linux and macOS; check both.
        os_keys.update({"linux", "darwin"})
    if not os_keys:
        # No platform markers means the lock claims universal compatibility:
        # verify the three operating systems Poetry supports.
        os_keys.update({"linux", "win32", "darwin"})

    # The x86_64 baseline is always covered: machine markers only describe
    # exceptional architectures, not the absence of other machines.
    raw_arches = values["platform_machine"] | {"x86_64"}

    profiles: list[PlatformProfile] = []
    seen: set[tuple[str, str]] = set()
    for os_key in sorted(os_keys):
        arches = {
            _normalize_arch(arch.lower(), os_key) for arch in raw_arches
        }
        for arch in sorted(arches):
            key = (os_key, arch)
            if key in seen:
                continue
            seen.add(key)
            sys_platform, platform_system, os_name = _OS_PROFILES[os_key]
            profiles.append(
                PlatformProfile(
                    sys_platform=sys_platform,
                    platform_system=platform_system,
                    os_name=os_name,
                    machine=arch,
                )
            )
    return profiles
