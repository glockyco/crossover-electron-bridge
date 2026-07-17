"""Validated game profiles and CrossOver path resolution.

Profiles are data only.  They select one of the adapters registered by the
package; they never contain import paths or executable code.  The loader is
intentionally strict so a malformed or untrusted profile cannot influence a
filesystem path or network bind address.
"""

from __future__ import annotations

import ipaddress
import json
import re
from dataclasses import dataclass, field
from importlib.resources import files
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any, Literal, Mapping

from .errors import ConfigurationError
from .paths import (
    bottle_root as _bottle_root,
    crossover_wine as _crossover_wine,
    steam_executable as _steam_executable,
    steam_install as _steam_install,
    steam_manifest as _steam_manifest,
)


SCHEMA_VERSION = 1
MODES = frozenset({"browser-only", "steam-electron"})
# These are code-registered IDs, not module names.  In particular, profile
# data is never allowed to select an arbitrary Python or Node implementation.
REGISTERED_ADAPTERS = frozenset({"static", "fractured_realms", "visseron_idle"})

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_BUILD_RE = re.compile(r"^[^\s]+$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True, slots=True)
class SteamBuild:
    """A supported Steam build and, for Electron games, its ASAR fingerprint."""

    build_id: str
    archive_sha256: str | None = None
    archive_size: int | None = None


@dataclass(frozen=True, slots=True)
class SteamConfig:
    """Steam identity and supported build set for a profile."""

    app_id: int
    install_dir: str
    supported_builds: tuple[SteamBuild, ...]


@dataclass(frozen=True, slots=True)
class RendererConfig:
    """Static renderer location and loopback browser origin."""

    assets: str
    bind_host: str
    browser_host: str
    port: int
    archive: str | None = None
    entry: str | None = None


@dataclass(frozen=True, slots=True)
class GameProfile:
    """Immutable, validated representation of one JSON game profile."""

    schema_version: int
    id: str
    display_name: str
    mode: Literal["browser-only", "steam-electron"]
    bottle: str
    steam: SteamConfig
    renderer: RendererConfig
    adapter: str
    state_key: str
    adapter_config: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )


@dataclass(frozen=True, slots=True)
class ResolvedProfile:
    """A profile paired with paths in a particular synthetic or real home."""

    profile: GameProfile
    bottle_root: Path
    manifest: Path
    install: Path
    steam_exe: Path
    wine: Path


def _fail(detail: str) -> ConfigurationError:
    return ConfigurationError(detail)


def _object(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _fail(f"{field_name} must be an object")
    return value


def _exact_keys(
    value: dict[str, Any], required: set[str], optional: set[str], field_name: str
) -> None:
    keys = set(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing:
        missing_text = ", ".join(sorted(missing))
        raise _fail(f"{field_name} is missing required key(s): {missing_text}")
    if unknown:
        unknown_text = ", ".join(sorted(unknown))
        raise _fail(f"{field_name} has unknown key(s): {unknown_text}")


def _string(value: Any, field_name: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str):
        raise _fail(f"{field_name} must be a string")
    if nonempty and not value:
        raise _fail(f"{field_name} must not be empty")
    if "\x00" in value:
        raise _fail(f"{field_name} contains a NUL character")
    return value


def _identifier(value: Any, field_name: str) -> str:
    value = _string(value, field_name)
    if not _IDENTIFIER_RE.fullmatch(value):
        raise _fail(f"{field_name} is not a safe identifier")
    return value


def _relative_path(value: Any, field_name: str) -> str:
    """Validate a profile path that is relative to a known install root."""

    value = _string(value, field_name)
    # Reject both POSIX and Windows absolute paths.  Backslashes are not valid
    # separators in the profile format; rejecting them also prevents a Windows
    # path from being interpreted differently by a downstream tool.
    if value.startswith(("/", "\\")) or _WINDOWS_DRIVE_RE.match(value):
        raise _fail(f"{field_name} must be a relative path")
    if "\\" in value:
        raise _fail(f"{field_name} must use POSIX separators")
    parts = PurePosixPath(value).parts
    if not parts or any(part in {".", ".."} for part in parts):
        raise _fail(f"{field_name} contains an unsafe path component")
    if PurePosixPath(value).as_posix() != value:
        raise _fail(f"{field_name} must use a normalized POSIX-relative path")
    return value


def _loopback_host(value: Any, field_name: str) -> str:
    value = _string(value, field_name)
    if value != value.strip() or "/" in value or "\\" in value:
        raise _fail(f"{field_name} must be a loopback host")
    lowered = value.lower()
    if lowered == "localhost":
        return value
    # Keep both socket binding and URL rendering unambiguous.  In particular,
    # reject bracketed or raw IPv6 rather than allowing one context to accept a
    # representation the other cannot use in ``http://<host>:<port>``.
    if value.startswith("[") or value.endswith("]"):
        raise _fail(f"{field_name} must be a loopback host")
    try:
        parsed = ipaddress.ip_address(value)
    except ValueError as exc:
        raise _fail(f"{field_name} must be a loopback host") from exc
    if parsed.version != 4 or not parsed.is_loopback:
        raise _fail(f"{field_name} must be a loopback host")
    return value


def _port(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise _fail(f"{field_name} must be an integer port in 1..65535")
    return value


def _freeze(value: Any, field_name: str) -> Any:
    """Recursively freeze JSON values used by adapter_config."""

    if isinstance(value, dict):
        return MappingProxyType(
            {str(key): _freeze(item, f"{field_name}.{key}") for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze(item, f"{field_name}[]") for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise _fail(f"{field_name} contains an unsupported JSON value")


def _builds(value: Any, mode: str) -> tuple[SteamBuild, ...]:
    if not isinstance(value, list) or not value:
        raise _fail("steam.supported_builds must be a non-empty array")
    result: list[SteamBuild] = []
    seen: set[str] = set()
    expected = {"build_id"}
    if mode == "steam-electron":
        expected |= {"archive_sha256", "archive_size"}
    for index, raw_build in enumerate(value):
        field_name = f"steam.supported_builds[{index}]"
        build = _object(raw_build, field_name)
        _exact_keys(build, expected, set(), field_name)
        build_id = _string(build["build_id"], f"{field_name}.build_id")
        if not _BUILD_RE.fullmatch(build_id):
            raise _fail(f"{field_name}.build_id must not contain whitespace")
        if build_id in seen:
            raise _fail(f"duplicate supported build {build_id}")
        seen.add(build_id)
        if mode == "steam-electron":
            archive_sha256 = _string(
                build["archive_sha256"], f"{field_name}.archive_sha256"
            )
            if not _SHA256_RE.fullmatch(archive_sha256):
                raise _fail(f"{field_name}.archive_sha256 must be a SHA-256 digest")
            archive_size = build["archive_size"]
            if (
                isinstance(archive_size, bool)
                or not isinstance(archive_size, int)
                or archive_size <= 0
            ):
                raise _fail(f"{field_name}.archive_size must be a positive integer")
            result.append(SteamBuild(build_id, archive_sha256.lower(), archive_size))
        else:
            result.append(SteamBuild(build_id))
    return tuple(result)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"invalid JSON constant {value}")


def _parse_profile(payload: Any, requested_id: str) -> GameProfile:
    raw = _object(payload, "profile")
    _exact_keys(
        raw,
        {
            "schema_version",
            "id",
            "display_name",
            "mode",
            "bottle",
            "steam",
            "renderer",
            "adapter",
            "state_key",
        },
        {"adapter_config"},
        "profile",
    )

    schema_version = raw["schema_version"]
    if (
        isinstance(schema_version, bool)
        or not isinstance(schema_version, int)
        or schema_version != SCHEMA_VERSION
    ):
        raise _fail(f"profile.schema_version must be {SCHEMA_VERSION}")
    profile_id = _identifier(raw["id"], "profile.id")
    if profile_id != requested_id:
        raise _fail("profile.id does not match requested profile")
    display_name = _string(raw["display_name"], "profile.display_name")
    mode = _string(raw["mode"], "profile.mode")
    if mode not in MODES:
        raise _fail("profile.mode must be browser-only or steam-electron")
    bottle = _relative_path(raw["bottle"], "profile.bottle")
    adapter = _identifier(raw["adapter"], "profile.adapter")
    if adapter not in REGISTERED_ADAPTERS:
        raise _fail(f"unknown adapter {adapter}")
    if mode == "steam-electron" and adapter == "static":
        raise _fail("adapter static is incompatible with steam-electron mode")
    state_key = _identifier(raw["state_key"], "profile.state_key")

    raw_steam = _object(raw["steam"], "steam")
    _exact_keys(
        raw_steam,
        {"app_id", "install_dir", "supported_builds"},
        set(),
        "steam",
    )
    app_id = raw_steam["app_id"]
    if isinstance(app_id, bool) or not isinstance(app_id, int) or app_id <= 0:
        raise _fail("steam.app_id must be a positive integer")
    install_dir = _relative_path(raw_steam["install_dir"], "steam.install_dir")
    supported_builds = _builds(raw_steam["supported_builds"], mode)
    steam = SteamConfig(app_id, install_dir, supported_builds)

    raw_renderer = _object(raw["renderer"], "renderer")
    renderer_required = {"assets", "bind_host", "browser_host", "port"}
    renderer_optional: set[str] = set()
    if mode == "steam-electron":
        renderer_required |= {"archive", "entry"}
    _exact_keys(raw_renderer, renderer_required, renderer_optional, "renderer")
    assets = _relative_path(raw_renderer["assets"], "renderer.assets")
    bind_host = _loopback_host(raw_renderer["bind_host"], "renderer.bind_host")
    browser_host = _loopback_host(raw_renderer["browser_host"], "renderer.browser_host")
    port = _port(raw_renderer["port"], "renderer.port")
    archive: str | None = None
    entry: str | None = None
    if mode == "steam-electron":
        archive = _relative_path(raw_renderer["archive"], "renderer.archive")
        entry = _relative_path(raw_renderer["entry"], "renderer.entry")
    renderer = RendererConfig(assets, bind_host, browser_host, port, archive, entry)

    adapter_config = raw.get("adapter_config", {})
    adapter_config = _object(adapter_config, "profile.adapter_config")
    frozen_config = _freeze(adapter_config, "profile.adapter_config")
    assert isinstance(frozen_config, MappingProxyType)
    return GameProfile(
        schema_version=SCHEMA_VERSION,
        id=profile_id,
        display_name=display_name,
        mode=mode,  # type: ignore[arg-type]
        bottle=bottle,
        steam=steam,
        renderer=renderer,
        adapter=adapter,
        state_key=state_key,
        adapter_config=frozen_config,
    )


def _profile_path(profile_id: str, profile_dir: Path | None) -> Any:
    if profile_dir is not None:
        return Path(profile_dir) / f"{profile_id}.json"
    return files("crossover_electron_bridge").joinpath("profiles", f"{profile_id}.json")


def load_profile(profile_id: str, profile_dir: Path | None = None) -> GameProfile:
    """Load and strictly validate a built-in or external profile.

    External lookup is intentionally exact: when ``profile_dir`` is supplied,
    only ``<profile_dir>/<profile_id>.json`` is read.  No fallback to built-ins
    or directory scanning occurs.
    """

    profile_id = _identifier(profile_id, "profile_id")
    source = _profile_path(profile_id, profile_dir)
    try:
        payload = json.loads(
            source.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except FileNotFoundError as exc:
        raise _fail(f"profile {profile_id} was not found") from exc
    except (OSError, UnicodeError, ValueError) as exc:
        raise _fail(f"could not read profile {profile_id}: {exc}") from exc
    return _parse_profile(payload, profile_id)


def resolve_profile(profile: GameProfile, home: Path = Path.home()) -> ResolvedProfile:
    """Resolve all approved CrossOver and Steam paths under *home*."""

    root = _bottle_root(profile.bottle, Path(home))
    return ResolvedProfile(
        profile=profile,
        bottle_root=root,
        manifest=_steam_manifest(profile.bottle, profile.steam.app_id, Path(home)),
        install=_steam_install(
            profile.bottle,
            profile.steam.app_id,
            profile.steam.install_dir,
            Path(home),
        ),
        steam_exe=_steam_executable(profile.bottle, Path(home)),
        wine=_crossover_wine(),
    )


__all__ = [
    "SCHEMA_VERSION",
    "MODES",
    "REGISTERED_ADAPTERS",
    "SteamBuild",
    "SteamConfig",
    "RendererConfig",
    "GameProfile",
    "ResolvedProfile",
    "load_profile",
    "resolve_profile",
]
