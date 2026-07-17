"""Transactional, build-bound ASAR patching and recovery.

The patch manager deliberately treats an installed archive as untrusted input.  It
only extracts a known, unpatched fingerprint for a supported Steam build, and it
never treats a marker as proof of a valid patch without independently validating
its metadata and immutable original backup.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import importlib.resources as package_resources
from pathlib import Path
import subprocess
import tempfile
from typing import Any, Callable, Iterator, Mapping, Protocol

from .atomic import atomic_copy, atomic_copy_if_absent, atomic_write_text
from .errors import OperationalError
from .profiles import GameProfile, ResolvedProfile, resolve_profile
from .steam import read_steam_manifest


_CHUNK_SIZE = 1024 * 1024
_METADATA_VERSION = 2
_STATE_ROOT = Path(".local/state/crossover-electron-bridge")
_METADATA_KEYS = {
    "metadata_version",
    "profile_id",
    "profile_revision",
    "marker",
    "steam_build_id",
    "timestamp",
    "original",
    "patched",
    "backup",
}
_ARCHIVE_RECORD_KEYS = {"sha256", "size"}
_BACKUP_RECORD_KEYS = {"path", "sha256", "size"}


@dataclass(frozen=True, slots=True)
class Fingerprint:
    """A streaming SHA-256/size fingerprint and optional marker result."""

    sha256: str
    size: int
    marker_found: bool


@dataclass(frozen=True, slots=True)
class PatchResult:
    """The result of a patch operation."""

    changed: bool
    archive: Path
    metadata: Path


@dataclass(frozen=True, slots=True)
class RuntimeResources:
    """Packaged Node resources copied into an extracted Electron app."""

    electron_host: Path
    node_adapter: Path


class AsarPatchAdapter(Protocol):
    revision: str

    def apply(
        self,
        extracted_root: Path,
        profile: GameProfile,
        resources: RuntimeResources,
    ) -> None: ...


def fingerprint(path: Path, marker: bytes | None = None) -> Fingerprint:
    """Hash *path* without loading it in memory, finding markers across chunks."""

    digest = hashlib.sha256()
    size = 0
    marker_found = False
    overlap = b""
    marker_length = len(marker) if marker is not None else 0
    with Path(path).open("rb") as source:
        while True:
            chunk = source.read(_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            if marker is not None and not marker_found:
                searchable = overlap + chunk
                marker_found = marker in searchable
                if marker_length > 1:
                    overlap = searchable[-(marker_length - 1) :]
    return Fingerprint(digest.hexdigest(), size, marker_found)


class AsarTool:
    """Small wrapper around the dependency-free-at-runtime Electron ASAR CLI."""

    def __init__(
        self,
        npx: str = "npx",
        runner: Callable[..., Any] | None = None,
    ) -> None:
        self.npx = npx
        self.runner = runner

    def _run(self, operation: str, source: Path, destination: Path) -> None:
        argv = [
            self.npx,
            "--yes",
            "@electron/asar",
            operation,
            str(source),
            str(destination),
        ]
        runner = self.runner if self.runner is not None else subprocess.run
        try:
            completed = runner(
                argv,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                shell=False,
            )
        except FileNotFoundError as exc:
            raise OperationalError(f"npx could not be executed: {self.npx}") from exc
        except subprocess.CalledProcessError as exc:
            output = (getattr(exc, "stderr", None) or getattr(exc, "stdout", None) or "")
            lines = str(output).strip().splitlines()
            suffix = f": {lines[-1]}" if lines else ""
            raise OperationalError(f"ASAR command failed{suffix}") from exc
        except OSError as exc:
            raise OperationalError(f"could not execute ASAR command: {exc}") from exc
        del completed

    def extract(self, archive: Path, destination: Path) -> None:
        self._run("extract", archive, destination)

    def pack(self, source: Path, archive: Path) -> None:
        self._run("pack", source, archive)


class PatchManager:
    """Build-bound patch and restore operations for ``steam-electron`` profiles."""

    def __init__(
        self,
        home: Path | None = None,
        asar_tool: AsarTool | None = None,
        clock: Callable[[], datetime | str] | datetime | str | None = None,
        hooks: Mapping[str, Callable[..., Any]] | Any | None = None,
        revision_resolver: Callable[[GameProfile], str] | None = None,
    ) -> None:
        self.home = Path.home() if home is None else Path(home)
        self.asar_tool = asar_tool if asar_tool is not None else AsarTool()
        self.clock = clock
        self.hooks = hooks
        self.revision_resolver = revision_resolver

    @staticmethod
    def _revision(adapter: AsarPatchAdapter) -> str:
        revision = getattr(adapter, "revision", None)
        if not isinstance(revision, str) or not revision:
            raise OperationalError("patch adapter has no valid revision")
        return revision

    def _hook(self, name: str, *args: Any) -> None:
        hook: Any = None
        if isinstance(self.hooks, Mapping):
            hook = self.hooks.get(name)
            if hook is None and name == "before_replace":
                hook = self.hooks.get("before_atomic_replace")
            if hook is None and name == "before_restore_replace":
                hook = self.hooks.get("before_atomic_replace")
            if hook is None and name == "before_restore_replace":
                hook = self.hooks.get("before_replace")
        elif self.hooks is not None:
            if callable(self.hooks):
                hook = self.hooks
            else:
                hook = getattr(self.hooks, name, None)
                if hook is None and name == "before_replace":
                    hook = getattr(self.hooks, "before_atomic_replace", None)
                if hook is None and name == "before_restore_replace":
                    hook = getattr(self.hooks, "before_atomic_replace", None)
                if hook is None and name == "before_restore_replace":
                    hook = getattr(self.hooks, "before_replace", None)
        if hook is None:
            return
        if not callable(hook):
            raise OperationalError(f"patch hook {name} is not callable")
        # Hooks in tests commonly need no arguments, while a path is useful to
        # integration hooks.  Supporting both keeps the production API narrow.
        if args:
            try:
                hook(*args)
                return
            except TypeError as first_error:
                try:
                    hook()
                    return
                except TypeError:
                    raise first_error
        hook()

    @property
    def state_root(self) -> Path:
        return self.home / _STATE_ROOT

    def state_dir(self, profile: GameProfile) -> Path:
        return self.state_root / profile.state_key

    def metadata_path(self, profile: GameProfile) -> Path:
        return self.state_dir(profile) / "metadata.json"

    @staticmethod
    def _archive_path(resolved: ResolvedProfile) -> Path:
        archive = resolved.profile.renderer.archive
        if archive is None:
            raise OperationalError(
                f"profile {resolved.profile.id} has no ASAR archive path"
            )
        return resolved.install / archive

    @staticmethod
    def _manifest(profile: GameProfile, resolved: ResolvedProfile) -> dict[str, str]:
        manifest = read_steam_manifest(resolved.manifest)
        if manifest["appid"] != str(profile.steam.app_id):
            raise OperationalError(
                f"Steam manifest AppID {manifest['appid']} does not match "
                f"{profile.steam.app_id} for {profile.id}"
            )
        if manifest["installdir"] != profile.steam.install_dir:
            raise OperationalError(
                f"Steam manifest install directory {manifest['installdir']} does not "
                f"match {profile.steam.install_dir} for {profile.id}"
            )
        return manifest

    @staticmethod
    def _supported_build(profile: GameProfile, build_id: str):
        for build in profile.steam.supported_builds:
            if build.build_id == build_id:
                return build
        raise OperationalError(f"Unsupported Steam build {build_id} for {profile.id}")

    @classmethod
    def _expected_original(cls, profile: GameProfile, build_id: str):
        build = cls._supported_build(profile, build_id)
        if build.archive_sha256 is None or build.archive_size is None:
            raise OperationalError(f"Unsupported Steam build {build_id} for {profile.id}")
        return Fingerprint(build.archive_sha256.lower(), build.archive_size, False)

    @staticmethod
    def _fingerprint_record(value: Any, description: str) -> tuple[str, int]:
        if not isinstance(value, dict) or set(value) != _ARCHIVE_RECORD_KEYS:
            raise OperationalError(f"state metadata has an invalid {description} record")
        digest = value.get("sha256")
        size = value.get("size")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdefABCDEF" for char in digest)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
        ):
            raise OperationalError(f"state metadata has an invalid {description} hash or size")
        return digest.lower(), size

    def _read_metadata(
        self,
        profile: GameProfile,
        revision: str,
    ) -> tuple[dict[str, Any], tuple[str, int], tuple[str, int], Path]:
        path = self.metadata_path(profile)
        try:
            with path.open("r", encoding="utf-8") as source:
                metadata = json.load(source)
        except (FileNotFoundError, OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise OperationalError(f"state metadata could not be read: {path}") from exc
        if not isinstance(metadata, dict) or set(metadata) != _METADATA_KEYS:
            raise OperationalError(f"state metadata has unexpected keys: {path}")
        if metadata.get("metadata_version") != _METADATA_VERSION:
            raise OperationalError(f"unsupported state metadata version: {path}")
        if metadata.get("profile_id") != profile.id:
            raise OperationalError("state metadata profile does not match this profile")
        if metadata.get("profile_revision") != revision:
            raise OperationalError("state metadata profile revision does not match this patch profile")
        if metadata.get("marker") != revision:
            raise OperationalError("state metadata marker does not match this patch profile")
        build_id = metadata.get("steam_build_id")
        if not isinstance(build_id, str) or not build_id:
            raise OperationalError("state metadata has no valid Steam build ID")
        timestamp = metadata.get("timestamp")
        if not isinstance(timestamp, str) or not timestamp:
            raise OperationalError("state metadata has no valid patch timestamp")
        try:
            parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise OperationalError("state metadata has no valid patch timestamp") from exc
        if parsed_timestamp.tzinfo is None:
            raise OperationalError("state metadata has no valid patch timestamp")
        original = self._fingerprint_record(metadata.get("original"), "original archive")
        patched = self._fingerprint_record(metadata.get("patched"), "patched archive")
        backup_value = metadata.get("backup")
        if not isinstance(backup_value, dict) or set(backup_value) != _BACKUP_RECORD_KEYS:
            raise OperationalError("state metadata has an invalid backup record")
        backup_path_value = backup_value.get("path")
        if not isinstance(backup_path_value, str) or not backup_path_value:
            raise OperationalError("state metadata has an invalid backup path")
        expected_path = f"backups/app.asar-{original[0]}.original"
        if backup_path_value != expected_path:
            raise OperationalError("state metadata points at an unexpected original backup")
        backup_digest, backup_size = self._fingerprint_record(
            {"sha256": backup_value.get("sha256"), "size": backup_value.get("size")},
            "backup",
        )
        if (backup_digest, backup_size) != original:
            raise OperationalError("state metadata backup does not match the original archive")
        backup_path = self.state_dir(profile) / backup_path_value
        return metadata, original, patched, backup_path

    @staticmethod
    def _verify_fingerprint(
        actual: Fingerprint,
        expected: tuple[str, int],
        *,
        marker: bool | None = None,
        description: str,
    ) -> None:
        if (actual.sha256, actual.size) != expected or (
            marker is not None and actual.marker_found is not marker
        ):
            raise OperationalError(f"{description}: fingerprint verification failed")

    @staticmethod
    def _resource_filename(adapter: str) -> str:
        return adapter.replace("_", "-") + ".cjs"

    @contextmanager
    def _runtime_resources(self, profile: GameProfile) -> Iterator[RuntimeResources]:
        root = package_resources.files("crossover_electron_bridge")
        electron_host = root.joinpath("runtime", "electron-host.cjs")
        node_adapter = root.joinpath("adapters", self._resource_filename(profile.adapter))
        with package_resources.as_file(electron_host) as host_path:
            with package_resources.as_file(node_adapter) as adapter_path:
                yield RuntimeResources(Path(host_path), Path(adapter_path))

    def _timestamp(self) -> str:
        value: datetime | str
        if self.clock is None:
            value = datetime.now(timezone.utc)
        elif callable(self.clock):
            value = self.clock()
        else:
            value = self.clock
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            else:
                value = value.astimezone(timezone.utc)
            return value.isoformat()
        if isinstance(value, str) and value:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise OperationalError("patch clock did not return a UTC timestamp") from exc
            if parsed.tzinfo is None:
                raise OperationalError("patch clock did not return a UTC timestamp")
            return parsed.astimezone(timezone.utc).isoformat()
        raise OperationalError("patch clock did not return a UTC timestamp")

    def _verify_backup(
        self,
        backup_path: Path,
        expected: tuple[str, int],
        marker: bytes,
    ) -> None:
        if backup_path.is_symlink() or not backup_path.is_file():
            raise OperationalError(f"immutable original backup not found: {backup_path}")
        try:
            actual = fingerprint(backup_path, marker)
        except OSError as exc:
            raise OperationalError(f"cannot read immutable original backup: {backup_path}") from exc
        self._verify_fingerprint(
            actual,
            expected,
            marker=False,
            description="immutable original backup",
        )

    def _ensure_backup(
        self,
        archive: Path,
        profile: GameProfile,
        original: Fingerprint,
        marker: bytes,
    ) -> Path:
        path = self.state_dir(profile) / "backups" / f"app.asar-{original.sha256}.original"
        path.parent.mkdir(parents=True, exist_ok=True)
        created = False
        if path.exists() or path.is_symlink():
            self._verify_backup(path, (original.sha256, original.size), marker)
            return path
        try:
            created = atomic_copy_if_absent(archive, path)
            self._verify_backup(path, (original.sha256, original.size), marker)
        except Exception:
            if created:
                try:
                    path.unlink()
                except OSError:
                    pass
            raise
        return path

    def _current_build_and_manifest(self, profile: GameProfile, resolved: ResolvedProfile) -> str:
        manifest = self._manifest(profile, resolved)
        build_id = manifest["buildid"]
        self._supported_build(profile, build_id)
        return build_id

    def _materialize_metadata(
        self,
        profile: GameProfile,
        revision: str,
        build_id: str,
        original: Fingerprint,
        patched: Fingerprint,
    ) -> tuple[dict[str, Any], str]:
        """Build and validate the complete metadata document before replacement."""

        timestamp = self._timestamp()
        try:
            parsed_timestamp = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except (AttributeError, TypeError, ValueError) as exc:
            raise OperationalError("patch metadata timestamp is invalid") from exc
        if parsed_timestamp.tzinfo is None or parsed_timestamp.utcoffset() is None:
            raise OperationalError("patch metadata timestamp is invalid")
        metadata: dict[str, Any] = {
            "metadata_version": _METADATA_VERSION,
            "profile_id": profile.id,
            "profile_revision": revision,
            "marker": revision,
            "steam_build_id": build_id,
            "timestamp": timestamp,
            "original": {"sha256": original.sha256, "size": original.size},
            "patched": {"sha256": patched.sha256, "size": patched.size},
            "backup": {
                "path": f"backups/app.asar-{original.sha256}.original",
                "sha256": original.sha256,
                "size": original.size,
            },
        }
        if set(metadata) != _METADATA_KEYS:
            raise OperationalError("patch metadata payload is incomplete")
        if not isinstance(revision, str) or not revision:
            raise OperationalError("patch metadata revision is invalid")
        if not isinstance(build_id, str) or not build_id:
            raise OperationalError("patch metadata Steam build is invalid")
        self._fingerprint_record(metadata["original"], "original archive")
        self._fingerprint_record(metadata["patched"], "patched archive")
        backup = metadata["backup"]
        if not isinstance(backup, dict) or set(backup) != _BACKUP_RECORD_KEYS:
            raise OperationalError("patch metadata backup payload is incomplete")
        serialized = json.dumps(metadata, indent=2, sort_keys=True) + "\n"
        try:
            decoded = json.loads(serialized)
        except (TypeError, ValueError) as exc:
            raise OperationalError("patch metadata payload could not be serialized") from exc
        if decoded != metadata:
            raise OperationalError("patch metadata payload did not round-trip")
        return metadata, serialized

    def _rollback_after_metadata_failure(
        self,
        archive: Path,
        original_candidate: Path,
        packed_fingerprint: Fingerprint,
        original: Fingerprint,
        marker: bytes,
        metadata_error: BaseException,
        failure_context: str = "metadata commit failed after archive replacement",
    ) -> None:
        """Restore a just-installed patch only if it was not concurrently changed."""

        try:
            live = fingerprint(archive, marker)
        except OSError as exc:
            raise OperationalError(
                f"{failure_context}; live archive is missing or unreadable, so rollback was not attempted: "
                f"{archive} ({exc})"
            ) from metadata_error
        if (
            (live.sha256, live.size) == (original.sha256, original.size)
            and not live.marker_found
        ):
            raise OperationalError(
                f"{failure_context}; verified original archive is already installed, so rollback was not needed: "
                f"{archive}"
            ) from metadata_error
        if live != packed_fingerprint or not live.marker_found:
            raise OperationalError(
                f"{failure_context}; archive changed concurrently, so rollback was not attempted: {archive}"
            ) from metadata_error
        try:
            candidate = fingerprint(original_candidate, marker)
            self._verify_fingerprint(
                candidate,
                (original.sha256, original.size),
                marker=False,
                description="verified rollback candidate",
            )
            # Candidate validation can yield to another process.  Recheck the
            # live path immediately before replacing it so a concurrent writer
            # is never overwritten by recovery.
            try:
                live_before_copy = fingerprint(archive, marker)
            except OSError as exc:
                raise OperationalError(
                    f"{failure_context}; live archive became missing or unreadable before rollback: "
                    f"{archive} ({exc})"
                ) from metadata_error
            if (
                (live_before_copy.sha256, live_before_copy.size)
                == (original.sha256, original.size)
                and not live_before_copy.marker_found
            ):
                raise OperationalError(
                    f"{failure_context}; verified original archive is already installed, so rollback was not needed: "
                    f"{archive}"
                ) from metadata_error
            if live_before_copy != packed_fingerprint or not live_before_copy.marker_found:
                raise OperationalError(
                    f"{failure_context}; archive changed concurrently before rollback, so it was not overwritten: "
                    f"{archive}"
                ) from metadata_error
            atomic_copy(original_candidate, archive)
            restored = fingerprint(archive, marker)
            self._verify_fingerprint(
                restored,
                (original.sha256, original.size),
                marker=False,
                description="rollback archive",
            )
        except Exception as exc:
            raise OperationalError(
                f"{failure_context}; rollback to the verified original failed: {exc}"
            ) from metadata_error
        raise OperationalError(
            f"{failure_context}; verified original restored: {metadata_error}"
        ) from metadata_error

    def patch(self, profile: GameProfile, adapter: AsarPatchAdapter) -> PatchResult:
        if profile.mode != "steam-electron":
            raise OperationalError(f"patch is unavailable for {profile.mode} profile {profile.id}")
        revision = self._revision(adapter)
        marker = revision.encode("utf-8")
        resolved = resolve_profile(profile, self.home)
        archive = self._archive_path(resolved)
        metadata_path = self.metadata_path(profile)

        build_id = self._current_build_and_manifest(profile, resolved)
        expected = self._expected_original(profile, build_id)
        try:
            live = fingerprint(archive, marker)
        except OSError as exc:
            raise OperationalError(f"cannot read installed archive: {archive}") from exc

        if live.marker_found:
            try:
                metadata, original_record, patched_record, backup_path = self._read_metadata(
                    profile, revision
                )
                if metadata["steam_build_id"] != build_id:
                    raise OperationalError("state metadata Steam build does not match installed build")
                self._verify_fingerprint(
                    live,
                    patched_record,
                    marker=True,
                    description="installed patched archive",
                )
                self._verify_backup(backup_path, original_record, marker)
                if original_record != (expected.sha256, expected.size):
                    raise OperationalError("state metadata original archive does not match supported build")
                # Re-read both the manifest and archive after state verification so
                # unchanged success always describes one stable live installation.
                final_build = self._current_build_and_manifest(profile, resolved)
                if final_build != build_id:
                    raise OperationalError("installed Steam build changed during idempotence check")
                final_live = fingerprint(archive, marker)
                self._verify_fingerprint(
                    final_live,
                    patched_record,
                    marker=True,
                    description="installed patched archive",
                )
                self._verify_backup(backup_path, original_record, marker)
            except OperationalError as exc:
                raise OperationalError(
                    "installed archive is marked patched but its recovery state cannot be verified: "
                    f"{exc}"
                ) from exc
            return PatchResult(False, archive, metadata_path)

        if (live.sha256, live.size) != (expected.sha256, expected.size):
            raise OperationalError(f"Unsupported Steam build {build_id} for {profile.id}")

        with tempfile.TemporaryDirectory(prefix=f"{profile.state_key}-patch-") as workspace_name:
            workspace = Path(workspace_name)
            extracted = workspace / "app"
            packed = workspace / "app.asar"
            original_candidate = workspace / "original.asar"
            self.asar_tool.extract(archive, extracted)
            with self._runtime_resources(profile) as resources:
                adapter.apply(extracted, profile, resources)
            self.asar_tool.pack(extracted, packed)
            try:
                packed_fingerprint = fingerprint(packed, marker)
            except OSError as exc:
                raise OperationalError(f"ASAR pack did not produce an archive: {packed}") from exc
            if not packed_fingerprint.marker_found:
                raise OperationalError("packed archive is missing the patch marker; refusing replacement")

            self._hook("before_replace", archive)
            current_build = self._current_build_and_manifest(profile, resolved)
            if current_build != build_id:
                raise OperationalError(f"Unsupported Steam build {current_build} for {profile.id}")
            current = fingerprint(archive, marker)
            if current != live:
                raise OperationalError(
                    "installed archive changed while preparing the patch; refusing replacement"
                )
            _metadata, metadata_text = self._materialize_metadata(
                profile,
                revision,
                build_id,
                live,
                packed_fingerprint,
            )
            backup_path = self._ensure_backup(archive, profile, live, marker)
            # Stage the already verified original now.  Rollback never reads the
            # mutable backup path after replacement.
            atomic_copy(backup_path, original_candidate)
            candidate = fingerprint(original_candidate, marker)
            self._verify_fingerprint(
                candidate,
                (live.sha256, live.size),
                marker=False,
                description="rollback candidate",
            )
            # Backup creation/staging are writes, so perform a final live/build
            # check before replacing the archive.
            current_build = self._current_build_and_manifest(profile, resolved)
            if current_build != build_id:
                raise OperationalError(f"Unsupported Steam build {current_build} for {profile.id}")
            current = fingerprint(archive, marker)
            if current != live:
                raise OperationalError(
                    "installed archive changed while preparing the patch; refusing replacement"
                )

            try:
                atomic_copy(packed, archive)
                installed = fingerprint(archive, marker)
                if installed != packed_fingerprint or not installed.marker_found:
                    raise OperationalError(
                        "atomically installed archive failed verification; state metadata was not written"
                    )
            except Exception as exc:
                self._rollback_after_metadata_failure(
                    archive,
                    original_candidate,
                    packed_fingerprint,
                    live,
                    marker,
                    exc,
                    failure_context="archive replacement failed after transition",
                )
            try:
                self._hook("after_replace", archive)
                self._verify_backup(backup_path, (live.sha256, live.size), marker)
                self._hook("before_metadata_commit", metadata_path)
                atomic_write_text(metadata_path, metadata_text, mode=0o600)
            except Exception as exc:
                self._rollback_after_metadata_failure(
                    archive,
                    original_candidate,
                    packed_fingerprint,
                    live,
                    marker,
                    exc,
                )
            try:
                self._hook("after_metadata_commit", metadata_path)
                # The immutable backup is part of the success contract, not just
                # an input checked before replacement.
                self._verify_backup(backup_path, (live.sha256, live.size), marker)
                final_build = self._current_build_and_manifest(profile, resolved)
                if final_build != build_id:
                    raise OperationalError("installed Steam build changed after patch commit")
                final_live = fingerprint(archive, marker)
                if final_live != packed_fingerprint or not final_live.marker_found:
                    raise OperationalError("installed archive changed after patch commit")
            except Exception as exc:
                self._rollback_after_metadata_failure(
                    archive,
                    original_candidate,
                    packed_fingerprint,
                    live,
                    marker,
                    exc,
                    failure_context="patch final verification failed after archive replacement",
                )
        return PatchResult(True, archive, metadata_path)

    def restore(self, profile: GameProfile) -> None:
        if profile.mode != "steam-electron":
            raise OperationalError(f"restore is unavailable for {profile.mode} profile {profile.id}")
        resolved = resolve_profile(profile, self.home)
        archive = self._archive_path(resolved)
        revision = self._revision_for_restore(profile)
        marker = revision.encode("utf-8")
        metadata, original, patched, backup_path = self._read_metadata(profile, revision)
        build_id = self._current_build_and_manifest(profile, resolved)
        if build_id != metadata["steam_build_id"]:
            raise OperationalError(
                "Steam build ID no longer matches the patched build; refusing restore"
            )
        expected = self._expected_original(profile, build_id)
        if original != (expected.sha256, expected.size):
            raise OperationalError("state metadata original archive does not match supported build")
        live = fingerprint(archive, marker)
        self._verify_fingerprint(live, patched, marker=True, description="installed patched archive")

        with tempfile.TemporaryDirectory(prefix=f"{profile.state_key}-restore-") as workspace_name:
            candidate_path = Path(workspace_name) / "original.asar"
            # Read the backup into a private, fsynced candidate and fingerprint
            # that candidate.  A later copy from the mutable backup path is never
            # allowed to install unverified bytes.
            self._verify_backup(backup_path, original, marker)
            atomic_copy(backup_path, candidate_path)
            candidate = fingerprint(candidate_path, marker)
            self._verify_fingerprint(
                candidate,
                original,
                marker=False,
                description="restore candidate",
            )

            self._hook("before_restore_replace", archive)
            self._verify_backup(backup_path, original, marker)
            current_build = self._current_build_and_manifest(profile, resolved)
            if current_build != build_id:
                raise OperationalError(
                    "Steam build ID changed during restore preflight; refusing replacement"
                )
            live_again = fingerprint(archive, marker)
            self._verify_fingerprint(
                live_again,
                patched,
                marker=True,
                description="installed archive changed during restore preflight",
            )
            atomic_copy(candidate_path, archive)
            restored = fingerprint(archive, marker)
            self._verify_fingerprint(restored, original, marker=False, description="restored archive")

    def _revision_for_restore(self, profile: GameProfile) -> str:
        """Resolve restore revision from trusted code, never state metadata."""

        if self.revision_resolver is not None:
            try:
                revision = self.revision_resolver(profile)
            except Exception as exc:
                raise OperationalError(
                    f"could not resolve registered patch revision for {profile.id}"
                ) from exc
            return self._revision_value(revision, profile.id)
        try:
            from .adapters import get_asar_adapter
            adapter = get_asar_adapter(profile)
            return self._revision(adapter)
        except Exception as exc:
            raise OperationalError(
                f"could not resolve registered patch revision for {profile.id}"
            ) from exc

    @staticmethod
    def _revision_value(value: Any, profile_id: str) -> str:
        if not isinstance(value, str) or not value:
            raise OperationalError(f"registered patch revision is invalid for {profile_id}")
        return value


__all__ = [
    "AsarPatchAdapter",
    "AsarTool",
    "Fingerprint",
    "PatchManager",
    "PatchResult",
    "RuntimeResources",
    "fingerprint",
]
