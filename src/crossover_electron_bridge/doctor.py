"""Read-only diagnostics for CrossOver Electron Bridge profiles."""

from __future__ import annotations

import errno
import http.client
import importlib.resources as package_resources
import json
import platform
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .adapters import get_asar_adapter, get_browser_adapter, is_registered
from .errors import CrossoverElectronBridgeError
from .patching import PatchManager, fingerprint
from .paths import crossover_wine
from .profiles import GameProfile, ResolvedProfile, resolve_profile
from .steam import read_steam_manifest


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One actionable, read-only doctor check."""

    level: Literal["PASS", "WARN", "FAIL"]
    name: str
    detail: str


_CHECK_PORT_TIMEOUT = 0.7
_HEALTH_RESPONSE_LIMIT = 64 * 1024


def _result(level: Literal["PASS", "WARN", "FAIL"], name: str, detail: str) -> CheckResult:
    return CheckResult(level, name, detail)


def _exists(path: Path, kind: str) -> tuple[bool, str]:
    if kind == "directory":
        present = path.is_dir()
    else:
        present = path.is_file()
    if present:
        return True, f"{kind.capitalize()} exists: {path}"
    return False, f"Missing {kind}: {path}"


def _manifest_result(
    profile: GameProfile,
    resolved: ResolvedProfile,
) -> tuple[CheckResult, dict[str, str] | None]:
    try:
        manifest = read_steam_manifest(resolved.manifest)
    except CrossoverElectronBridgeError as exc:
        return _result("FAIL", "manifest", str(exc)), None
    except Exception as exc:  # pragma: no cover - defensive filesystem boundary
        return _result("FAIL", "manifest", f"Could not read Steam manifest {resolved.manifest}: {exc}"), None

    expected = {
        "name": profile.display_name,
        "appid": str(profile.steam.app_id),
        "installdir": profile.steam.install_dir,
    }
    mismatches = [
        f"{field}={manifest[field]!r} (expected {value!r})"
        for field, value in expected.items()
        if manifest.get(field) != value
    ]
    if mismatches:
        detail = f"Steam manifest identity mismatch for {profile.id}: " + "; ".join(mismatches)
        return _result("FAIL", "manifest", detail), manifest
    return (
        _result(
            "PASS",
            "manifest",
            f"Steam manifest matches {profile.display_name}, AppID {profile.steam.app_id}, "
            f"install directory {profile.steam.install_dir}, build {manifest['buildid']}",
        ),
        manifest,
    )


def _supported_build(profile: GameProfile, build_id: str) -> Any | None:
    return next(
        (build for build in profile.steam.supported_builds if build.build_id == build_id),
        None,
    )


def _resource_is_file(*parts: str) -> bool:
    """Check a packaged resource without extracting or writing to the install."""

    try:
        resource = package_resources.files("crossover_electron_bridge")
        for part in parts:
            resource = resource.joinpath(part)
        try:
            if resource.is_file():
                return True
        except (AttributeError, OSError):
            pass
        with package_resources.as_file(resource) as path:
            return Path(path).is_file()
    except (FileNotFoundError, OSError, TypeError, ValueError):
        return False


def _port_check(profile: GameProfile) -> CheckResult:
    """Probe a configured port, distinguishing free from same-profile listeners."""

    host = profile.renderer.bind_host
    browser_host = profile.renderer.browser_host
    port = profile.renderer.port
    connection: http.client.HTTPConnection | None = None
    try:
        connection = http.client.HTTPConnection(host, port, timeout=_CHECK_PORT_TIMEOUT)
        connection.request(
            "GET",
            "/health",
            headers={"Host": f"{browser_host}:{port}", "Connection": "close"},
        )
        response = connection.getresponse()
        payload = response.read(_HEALTH_RESPONSE_LIMIT + 1)
        if len(payload) > _HEALTH_RESPONSE_LIMIT:
            return _result("FAIL", "port", f"Port {port} is in use by another process")
        try:
            decoded = json.loads(payload.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            decoded = None
        expected = {
            "ok": True,
            "service": profile.id,
            "host": profile.renderer.bind_host,
            "port": port,
        }
        if response.status == 200 and decoded == expected:
            return _result("PASS", "port", f"Port {port} is serving {profile.id} /health")
        return _result("FAIL", "port", f"Port {port} is in use by another process")
    except OSError as exc:
        if isinstance(exc, ConnectionRefusedError) or exc.errno == errno.ECONNREFUSED:
            return _result("PASS", "port", f"Port {port} is available")
        if exc.errno in {errno.ETIMEDOUT, errno.EHOSTUNREACH, errno.ENETUNREACH}:
            return _result("FAIL", "port", f"Could not determine whether port {port} is available: {exc}")
        return _result("FAIL", "port", f"Could not probe port {port}: {exc}")
    except (http.client.HTTPException, TimeoutError, ValueError) as exc:
        return _result("FAIL", "port", f"Could not probe port {port}: {exc}")
    finally:
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass


def _archive_state_result(
    profile: GameProfile,
    resolved: ResolvedProfile,
    manifest: dict[str, str] | None,
    adapter: Any | None,
    home: Path,
) -> CheckResult:
    archive_name = profile.renderer.archive
    if archive_name is None:
        return _result("FAIL", "archive", f"Profile {profile.id} has no ASAR archive path")
    archive = resolved.install / archive_name
    if not archive.is_file():
        return _result("FAIL", "archive", f"Missing ASAR archive: {archive}")
    if manifest is None:
        return _result("FAIL", "archive", f"Cannot validate ASAR build because manifest is unavailable: {archive}")

    build_id = manifest.get("buildid", "")
    supported = _supported_build(profile, build_id)
    if supported is None or supported.archive_sha256 is None or supported.archive_size is None:
        return _result("FAIL", "archive", f"Unsupported Steam build {build_id} for {profile.id}")

    revision = getattr(adapter, "revision", None)
    marker = revision.encode("utf-8") if isinstance(revision, str) and revision else None
    try:
        live = fingerprint(archive, marker)
    except OSError as exc:
        return _result("FAIL", "archive", f"Could not fingerprint ASAR archive {archive}: {exc}")

    expected = (supported.archive_sha256.lower(), supported.archive_size)
    if (live.sha256, live.size) == expected and not live.marker_found:
        return _result("PASS", "archive", f"ASAR matches supported original fingerprint ({live.sha256}, {live.size} bytes)")

    if not live.marker_found:
        return _result(
            "FAIL",
            "archive",
            f"ASAR fingerprint mismatch for {profile.id}: got {live.sha256}, {live.size} bytes; "
            f"expected original {expected[0]}, {expected[1]} bytes",
        )
    if not isinstance(revision, str) or not revision:
        return _result("FAIL", "archive", "ASAR contains a patch marker but no registered patch revision is available")

    manager = PatchManager(home=home)
    try:
        metadata, original, patched, backup_path = manager._read_metadata(profile, revision)
        if metadata.get("steam_build_id") != build_id:
            raise ValueError("state metadata Steam build does not match the installed build")
        if original != expected:
            raise ValueError("state metadata original fingerprint does not match the supported build")
        manager._verify_fingerprint(
            live,
            patched,
            marker=True,
            description="installed patched archive",
        )
        manager._verify_backup(backup_path, original, revision.encode("utf-8"))
    except Exception as exc:
        return _result(
            "FAIL",
            "archive",
            f"ASAR contains the patch marker but its recorded recovery state is invalid: {exc}",
        )
    return _result(
        "PASS",
        "archive",
        f"ASAR matches the verified patched state for build {build_id}; immutable original backup is valid",
    )


def run_doctor(profile: GameProfile) -> list[CheckResult]:
    """Run all non-mutating checks for *profile*.

    The function only reads profile/install/state files and probes the configured
    loopback port.  It never binds, patches, restores, creates, or removes files.
    """

    home = Path.home()
    resolved = resolve_profile(profile, home)
    results: list[CheckResult] = []

    system = platform.system()
    if system == "Darwin":
        results.append(_result("PASS", "macos", "Running on macOS (Darwin)"))
    else:
        results.append(_result("FAIL", "macos", f"CrossOver requires macOS (Darwin), found {system or 'unknown platform'}"))

    wine = resolved.wine
    if not wine.is_file():
        fallback = crossover_wine()
        if fallback != wine and fallback.is_file():
            wine = fallback
    if wine.is_file():
        results.append(_result("PASS", "wine", f"CrossOver wine executable found: {wine}"))
    else:
        results.append(_result("FAIL", "wine", f"Missing CrossOver wine executable: {wine}"))

    bottle_ok, bottle_detail = _exists(resolved.bottle_root, "directory")
    results.append(_result("PASS" if bottle_ok else "FAIL", "bottle", bottle_detail))
    steam_ok, steam_detail = _exists(resolved.steam_exe, "file")
    results.append(_result("PASS" if steam_ok else "FAIL", "steam-exe", steam_detail))

    manifest_result, manifest = _manifest_result(profile, resolved)
    results.append(manifest_result)

    if profile.mode == "browser-only":
        assets = resolved.install / profile.renderer.assets
        assets_ok, assets_detail = _exists(assets, "directory")
        results.append(_result("PASS" if assets_ok else "FAIL", "assets", assets_detail))

    try:
        registered = is_registered(profile.adapter)
        if not registered:
            raise ValueError(f"unknown adapter {profile.adapter}")
        if profile.mode == "browser-only":
            get_browser_adapter(profile)
            adapter = None
        else:
            adapter = get_asar_adapter(profile)
        detail = f"Adapter {profile.adapter} is registered for {profile.mode}"
        results.append(_result("PASS", "adapter", detail))
    except Exception as exc:
        adapter = None
        results.append(_result("FAIL", "adapter", f"Adapter {profile.adapter} is unavailable for {profile.mode}: {exc}"))

    build_id = manifest.get("buildid") if manifest is not None else None
    if build_id is None:
        results.append(_result("FAIL", "steam-build", "Steam build cannot be checked because the manifest is unavailable"))
    else:
        supported = _supported_build(profile, build_id)
        if supported is not None:
            results.append(_result("PASS", "steam-build", f"Steam build {build_id} is supported by {profile.id}"))
        elif profile.mode == "browser-only":
            results.append(_result("WARN", "steam-build", f"Steam build {build_id} is not listed for {profile.id}; browser-only mode does not patch ASAR files"))
        else:
            results.append(_result("FAIL", "steam-build", f"Unsupported Steam build {build_id} for {profile.id}"))

    if profile.mode == "steam-electron":
        archive_result = _archive_state_result(profile, resolved, manifest, adapter, home)
        results.append(archive_result)
        if archive_result.level == "PASS":
            results.append(
                _result(
                    "PASS",
                    "assets",
                    f"Profile assets {profile.renderer.assets} are contained in the verified ASAR package",
                )
            )
        else:
            results.append(
                _result(
                    "FAIL",
                    "assets",
                    f"Cannot verify internal assets {profile.renderer.assets}: {archive_result.detail}",
                )
            )

        npx = shutil.which("npx")
        if npx:
            results.append(_result("PASS", "npx", f"npx is available: {npx}"))
        else:
            results.append(_result("FAIL", "npx", "npx is not available on PATH; ASAR patching cannot run"))

        adapter_filename = profile.adapter.replace("_", "-") + ".cjs"
        runtime_ok = _resource_is_file("runtime", "electron-host.cjs") and _resource_is_file("adapters", adapter_filename)
        if runtime_ok:
            results.append(_result("PASS", "runtime", "Packaged Electron host and Node adapter resources are available"))
        else:
            results.append(_result("FAIL", "runtime", "Packaged Electron host or Node adapter resource is missing"))

        if adapter is not None and isinstance(getattr(adapter, "revision", None), str) and adapter.revision:
            results.append(_result("PASS", "patch-adapter", f"Patch adapter revision registered: {adapter.revision}"))
        else:
            results.append(_result("FAIL", "patch-adapter", f"Patch adapter revision is unavailable for {profile.id}"))

    results.append(_port_check(profile))
    return results


__all__ = ["CheckResult", "run_doctor"]
