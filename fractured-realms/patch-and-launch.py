#!/usr/bin/env python3
"""Patch and launch the Fractured Realms Demo through CrossOver's Steam bottle."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


HOME = Path.home()
BOTTLE = HOME / "Library/Application Support/CrossOver/Bottles/Steam"
ARCHIVE = (
    BOTTLE
    / "drive_c/Program Files (x86)/Steam/steamapps/common/Fractured Realms Demo/resources/app.asar"
)
STEAM_MANIFEST = BOTTLE / "drive_c/Program Files (x86)/Steam/steamapps/appmanifest_4750500.acf"
STATE_DIR = HOME / ".local/state/crossover-browser-games/fractured-realms"
BACKUPS_DIR = STATE_DIR / "backups"
METADATA = STATE_DIR / "metadata.json"
BROWSER_MODULE = Path(__file__).resolve().with_name("browser-host.cjs")
WINE = Path("/Applications/CrossOver.app/Contents/SharedSupport/CrossOver/bin/wine")
STEAM_EXE = BOTTLE / "drive_c/Program Files (x86)/Steam/Steam.exe"
MARKER = b"FRACTURED_REALMS_CROSSOVER_BROWSER_V1"
MARKER_TEXT = MARKER.decode("ascii")
METADATA_VERSION = 1
PROFILE_REVISION = MARKER_TEXT
ARCHIVE_CHUNK_SIZE = 1024 * 1024


class CrossoverError(RuntimeError):
    """An actionable patcher or launcher failure."""


def fail_if_missing(path: Path, description: str, *, executable: bool = False) -> None:
    if not path.is_file():
        raise CrossoverError(f"{description} not found: {path}")
    if executable and not os.access(path, os.X_OK):
        raise CrossoverError(f"{description} is not executable: {path}")


def fail_if_directory_missing(path: Path, description: str) -> None:
    if not path.is_dir():
        raise CrossoverError(f"{description} not found: {path}")


def sync_directory(directory: Path) -> None:
    """Persist a directory entry update after an atomic rename or link."""
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def copy_to_temporary(source: Path, temporary: Path) -> None:
    shutil.copy2(source, temporary)
    with temporary.open("rb") as copied:
        os.fsync(copied.fileno())


def atomic_copy(source: Path, destination: Path) -> None:
    """Copy a file beside its destination, then replace it atomically."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.tmp-", dir=destination.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        copy_to_temporary(source, temporary)
        os.replace(temporary, destination)
        sync_directory(destination.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def atomic_copy_if_absent(source: Path, destination: Path) -> bool:
    """Install a new file without ever replacing an existing destination."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.tmp-", dir=destination.parent
    )
    os.close(fd)
    temporary = Path(temporary_name)
    try:
        copy_to_temporary(source, temporary)
        try:
            os.link(temporary, destination)
        except FileExistsError:
            return False
        sync_directory(destination.parent)
        return True
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def stream_fingerprint(path: Path, marker: bytes | None = MARKER) -> tuple[str, int, bool]:
    """Hash a file and optionally find a marker without loading it in memory."""
    digest = hashlib.sha256()
    size = 0
    marker_found = False
    overlap = b""
    with path.open("rb") as source:
        while True:
            chunk = source.read(ARCHIVE_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            size += len(chunk)
            if marker is not None and not marker_found:
                searchable = overlap + chunk
                marker_found = marker in searchable
                if len(marker) > 1:
                    overlap = searchable[-(len(marker) - 1) :]
    return digest.hexdigest(), size, marker_found


def fingerprint_text(fingerprint: tuple[str, int, bool]) -> str:
    digest, size, _ = fingerprint
    return f"sha256={digest}, size={size} bytes"


def fingerprint_matches(record: dict[str, object], fingerprint: tuple[str, int, bool]) -> bool:
    return record.get("sha256") == fingerprint[0] and record.get("size") == fingerprint[1]


def asar_command(npx: str, *arguments: str) -> None:
    try:
        completed = subprocess.run(
            [npx, "--yes", "@electron/asar", *arguments],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise CrossoverError(f"npx could not be executed: {npx}") from exc
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip().splitlines()
        suffix = f": {detail[-1]}" if detail else ""
        raise CrossoverError(f"ASAR command failed{suffix}") from exc
    # Keep the local variable useful to debuggers without printing noisy npx output.
    del completed


def patch_main(main_file: Path) -> None:
    try:
        source = main_file.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CrossoverError(f"cannot read Electron entry point: {main_file}") from exc

    if "const crossoverBrowserMode = process.argv.includes('--crossover-browser');" in source:
        raise CrossoverError(f"Electron entry point is already patched: {main_file}")

    newline = "\r\n" if "\r\n" in source else "\n"
    mode_import = (
        "const crossoverBrowserMode = process.argv.includes('--crossover-browser');"
        f" // {MARKER_TEXT}{newline}"
    )
    fs_import = "const fs   = require('fs');"
    if source.count(fs_import) != 1:
        raise CrossoverError(f"unexpected Electron entry point imports: {main_file}")
    source = source.replace(fs_import, fs_import + newline + mode_import, 1)

    original = (
        "app.whenReady().then(() => {\n"
        "  createWindow();\n"
        "  app.on('activate', () => {\n"
        "    if (BrowserWindow.getAllWindows().length === 0) createWindow();\n"
        "  });\n"
        "});"
    )
    replacement = (
        "app.whenReady().then(async () => {\n"
        "  if (crossoverBrowserMode) {\n"
        "    await require('./crossover-browser.cjs').start({ app, shell, path, fs });\n"
        "    return;\n"
        "  }\n"
        "  createWindow();\n"
        "  app.on('activate', () => {\n"
        "    if (BrowserWindow.getAllWindows().length === 0) createWindow();\n"
        "  });\n"
        "});"
    )
    if "\r\n" in source:
        original = original.replace("\n", "\r\n")
        replacement = replacement.replace("\n", "\r\n")
    if source.count(original) != 1:
        raise CrossoverError(f"unexpected app.whenReady block in {main_file}")
    source = source.replace(original, replacement, 1)

    try:
        main_file.write_text(source, encoding="utf-8", newline="")
    except OSError as exc:
        raise CrossoverError(f"cannot write patched Electron entry point: {main_file}") from exc


def steam_build_id() -> str:
    try:
        manifest = STEAM_MANIFEST.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CrossoverError(f"cannot read Steam app manifest: {STEAM_MANIFEST}") from exc
    matches = re.findall(r'"buildid"\s+"([^"]+)"', manifest, flags=re.IGNORECASE)
    if len(matches) != 1 or not matches[0]:
        raise CrossoverError(
            f"Steam app manifest has no unambiguous build ID: {STEAM_MANIFEST}"
        )
    return matches[0]


def read_metadata() -> dict[str, object]:
    try:
        with METADATA.open("r", encoding="utf-8") as source:
            metadata = json.load(source)
    except FileNotFoundError as exc:
        raise CrossoverError(f"state metadata not found: {METADATA}") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CrossoverError(f"cannot read state metadata: {METADATA}") from exc
    if not isinstance(metadata, dict):
        raise CrossoverError(f"state metadata is not an object: {METADATA}")
    return metadata


def metadata_archive_record(value: object, description: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise CrossoverError(f"state metadata has no valid {description} record")
    digest = value.get("sha256")
    size = value.get("size")
    if (
        not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or type(size) is not int
        or size < 0
    ):
        raise CrossoverError(f"state metadata has an invalid {description} hash or size")
    return {"sha256": digest, "size": size}


def metadata_state(metadata: dict[str, object]) -> tuple[
    str, dict[str, object], dict[str, object], dict[str, object], Path
]:
    if metadata.get("metadata_version") != METADATA_VERSION:
        raise CrossoverError(f"unsupported state metadata version: {METADATA}")
    if metadata.get("marker") != MARKER_TEXT:
        raise CrossoverError("state metadata marker does not match this patch profile")
    if metadata.get("profile_revision") != PROFILE_REVISION:
        raise CrossoverError("state metadata profile revision does not match this patch profile")
    timestamp = metadata.get("timestamp")
    if not isinstance(timestamp, str) or not timestamp:
        raise CrossoverError("state metadata has no valid patch timestamp")
    build_id = metadata.get("steam_build_id")
    if not isinstance(build_id, str) or not build_id:
        raise CrossoverError("state metadata has no valid Steam build ID")

    original = metadata_archive_record(metadata.get("original"), "original archive")
    patched = metadata_archive_record(metadata.get("patched"), "patched archive")
    backup = metadata_archive_record(metadata.get("backup"), "backup")
    if backup != original:
        raise CrossoverError("state metadata backup hash or size does not match the original archive")

    backup_entry = metadata.get("backup")
    assert isinstance(backup_entry, dict)
    backup_name = backup_entry.get("path")
    expected_name = f"backups/app.asar-{original['sha256']}.original"
    if backup_name != expected_name:
        raise CrossoverError("state metadata points at an unexpected original backup")
    backup_path = STATE_DIR / expected_name
    return build_id, original, patched, backup, backup_path


def verify_backup(backup_path: Path, backup_record: dict[str, object]) -> None:
    if not backup_path.is_file():
        raise CrossoverError(f"immutable original backup not found: {backup_path}")
    try:
        fingerprint = stream_fingerprint(backup_path)
    except OSError as exc:
        raise CrossoverError(f"cannot read immutable original backup: {backup_path}") from exc
    if not fingerprint_matches(backup_record, fingerprint):
        raise CrossoverError(
            "immutable original backup hash or size does not match state metadata "
            f"({backup_path})"
        )
    if fingerprint[2]:
        raise CrossoverError(f"immutable original backup contains the patch marker: {backup_path}")


def ensure_original_backup(original: tuple[str, int, bool]) -> Path:
    digest, _, _ = original
    backup_path = BACKUPS_DIR / f"app.asar-{digest}.original"
    if backup_path.exists():
        if not backup_path.is_file():
            raise CrossoverError(f"immutable original backup is not a file: {backup_path}")
        try:
            existing = stream_fingerprint(backup_path)
        except OSError as exc:
            raise CrossoverError(f"cannot read immutable original backup: {backup_path}") from exc
        if existing[:2] != original[:2]:
            raise CrossoverError(
                "immutable original backup has been changed; refusing to overwrite it: "
                f"{backup_path}"
            )
        if existing[2]:
            raise CrossoverError(f"immutable original backup contains the patch marker: {backup_path}")
        return backup_path

    try:
        atomic_copy_if_absent(ARCHIVE, backup_path)
        installed = stream_fingerprint(backup_path)
    except OSError as exc:
        raise CrossoverError(f"cannot create immutable original backup: {backup_path}") from exc
    if installed[:2] != original[:2]:
        raise CrossoverError(
            "archive changed while creating its immutable backup; refusing to patch"
        )
    if installed[2]:
        raise CrossoverError(f"immutable original backup contains the patch marker: {backup_path}")
    return backup_path


def write_metadata(metadata: dict[str, object]) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{METADATA.name}.tmp-", dir=STATE_DIR
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as destination:
            json.dump(metadata, destination, indent=2, sort_keys=True)
            destination.write("\n")
            destination.flush()
            os.fsync(destination.fileno())
        os.replace(temporary, METADATA)
        sync_directory(STATE_DIR)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def restore_refusal(reason: str) -> None:
    raise CrossoverError(
        "Restore refused; no files were changed.\n"
        f"Doctor: {reason}\n"
        f"Doctor: inspect state metadata at {METADATA} and the Steam archive before retrying."
    )


def patch_archive() -> bool:
    fail_if_missing(ARCHIVE, "installed app.asar")
    build_id = steam_build_id()
    try:
        live = stream_fingerprint(ARCHIVE)
    except OSError as exc:
        raise CrossoverError(f"cannot read installed archive: {ARCHIVE}") from exc

    if live[2]:
        try:
            metadata = read_metadata()
            recorded_build, _, patched, backup, backup_path = metadata_state(metadata)
            if recorded_build != build_id:
                raise CrossoverError(
                    "Steam build ID changed while the installed archive still has the patch; "
                    "refusing to treat it as already patched"
                )
            if not fingerprint_matches(patched, live):
                raise CrossoverError(
                    "installed archive marker is present but its hash or size does not match state metadata"
                )
            verify_backup(backup_path, backup)
        except CrossoverError as exc:
            raise CrossoverError(
                "installed archive is marked patched but its recovery state cannot be verified. "
                f"Doctor: {exc}"
            ) from exc
        print(f"Already patched: {ARCHIVE}")
        return False

    fail_if_missing(BROWSER_MODULE, "browser host module")
    npx = shutil.which("npx")
    if not npx:
        raise CrossoverError("npx not found on PATH (install Node.js before patching)")

    original = live
    backup_path = ensure_original_backup(original)
    with tempfile.TemporaryDirectory(prefix="fractured-realms-crossover-") as workspace_name:
        workspace = Path(workspace_name)
        extracted = workspace / "app"
        asar_command(npx, "extract", str(ARCHIVE), str(extracted))

        main_file = extracted / "electron/main.cjs"
        fail_if_missing(main_file, "extracted Electron entry point")
        electron_dir = extracted / "electron"
        fail_if_directory_missing(electron_dir, "extracted Electron directory")
        shutil.copy2(BROWSER_MODULE, electron_dir / "crossover-browser.cjs")
        patch_main(main_file)

        packed = workspace / "app.asar"
        asar_command(npx, "pack", str(extracted), str(packed))
        try:
            packed_fingerprint = stream_fingerprint(packed)
        except OSError as exc:
            raise CrossoverError(f"ASAR pack did not produce an archive: {packed}") from exc
        if not packed_fingerprint[2]:
            raise CrossoverError(
                "packed archive is missing the crossover marker; refusing replacement"
            )

        try:
            current = stream_fingerprint(ARCHIVE)
        except OSError as exc:
            raise CrossoverError(f"cannot re-read installed archive: {ARCHIVE}") from exc
        if current != original:
            raise CrossoverError(
                "installed archive changed while preparing the patch; refusing replacement"
            )
        if steam_build_id() != build_id:
            raise CrossoverError(
                "Steam build ID changed while preparing the patch; refusing replacement"
            )
        atomic_copy(packed, ARCHIVE)
        try:
            installed = stream_fingerprint(ARCHIVE)
        except OSError as exc:
            raise CrossoverError(f"cannot verify patched archive: {ARCHIVE}") from exc
        if installed[:2] != packed_fingerprint[:2] or not installed[2]:
            raise CrossoverError(
                "atomically installed archive failed verification; state metadata was not written"
            )

    metadata = {
        "metadata_version": METADATA_VERSION,
        "profile_revision": PROFILE_REVISION,
        "marker": MARKER_TEXT,
        "steam_build_id": build_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "original": {"sha256": original[0], "size": original[1]},
        "patched": {"sha256": packed_fingerprint[0], "size": packed_fingerprint[1]},
        "backup": {
            "path": f"backups/{backup_path.name}",
            "sha256": original[0],
            "size": original[1],
        },
    }
    # This is intentionally after the archive replacement: a crash before this
    # point leaves a complete archive, never a partially written app.asar.
    write_metadata(metadata)
    print(f"Patched: {ARCHIVE}")
    return True


def restore_archive() -> None:
    try:
        metadata = read_metadata()
        recorded_build, original, patched, backup, backup_path = metadata_state(metadata)
    except CrossoverError as exc:
        restore_refusal(str(exc))

    try:
        current_build = steam_build_id()
    except CrossoverError as exc:
        restore_refusal(str(exc))
    if current_build != recorded_build:
        restore_refusal(
            "Steam build ID no longer matches the patched build; Steam may have updated the game"
        )
    try:
        live = stream_fingerprint(ARCHIVE)
    except OSError as exc:
        restore_refusal(f"cannot read installed archive {ARCHIVE}: {exc}")
    if not fingerprint_matches(patched, live) or not live[2]:
        restore_refusal(
            "installed archive does not match the recorded patched archive; Steam may have "
            "updated it since patching. "
            f"Expected {fingerprint_text((patched['sha256'], patched['size'], True))}; "
            f"found {fingerprint_text(live)}"
        )

    try:
        verify_backup(backup_path, backup)
    except CrossoverError as exc:
        restore_refusal(str(exc))

    # Hash immediately before replacement as well as during preflight. This
    # closes the normal Steam-update window without ever trusting a stale backup.
    try:
        live_again = stream_fingerprint(ARCHIVE)
    except OSError as exc:
        restore_refusal(f"cannot re-read installed archive {ARCHIVE}: {exc}")
    if not fingerprint_matches(patched, live_again) or not live_again[2]:
        restore_refusal(
            "installed archive changed during restore preflight; no replacement was attempted"
        )

    atomic_copy(backup_path, ARCHIVE)
    try:
        restored = stream_fingerprint(ARCHIVE)
    except OSError as exc:
        raise CrossoverError(f"cannot verify restored archive: {ARCHIVE}") from exc
    if not fingerprint_matches(original, restored):
        raise CrossoverError(
            "restore verification failed after atomic replacement; state metadata and backup were retained"
        )
    print(f"Restored: {ARCHIVE}")


def launch() -> None:
    fail_if_missing(WINE, "CrossOver wine binary", executable=True)
    fail_if_missing(STEAM_EXE, "bottled Steam.exe")
    argv = [
        str(WINE),
        "--bottle",
        "Steam",
        "--no-wait",
        str(STEAM_EXE),
        "-applaunch",
        "4750500",
        "--crossover-browser",
    ]
    try:
        subprocess.Popen(argv, shell=False)
    except OSError as exc:
        raise CrossoverError(f"could not launch Steam through CrossOver: {exc}") from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--patch-only", action="store_true", help="patch without launching Steam")
    modes.add_argument("--restore", action="store_true", help="restore the latest unpatched archive")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        if args.restore:
            restore_archive()
            return 0
        patch_archive()
        if args.patch_only:
            return 0
        launch()
    except (CrossoverError, OSError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
