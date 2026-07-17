"""Durable filesystem primitives used by the bridge state and patch stores."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import BinaryIO, TextIO


def sync_directory(path: Path) -> None:
    """Flush a directory's namespace changes to stable storage.

    Directory fsync is supported by the filesystems used by the bridge.  Any
    failure is intentionally propagated: callers must not report a durable
    update when the containing directory could not be synchronized.
    """

    flags = os.O_RDONLY
    directory_flag = getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags | directory_flag)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _temporary_file(directory: Path, name: str) -> tuple[int, Path]:
    """Create a uniquely named temporary file beside *name*."""

    fd, temporary = tempfile.mkstemp(
        prefix=f".{name}.",
        suffix=".tmp",
        dir=directory,
    )
    return fd, Path(temporary)


def _remove_temporary(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        # Preserve the operation's original exception.  Cleanup is best effort
        # when the filesystem itself has become unavailable or read-only.
        pass


def _flush_file(stream: BinaryIO | TextIO) -> None:
    stream.flush()
    os.fsync(stream.fileno())


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """Write UTF-8 text through a same-directory temporary and replace.

    The requested mode is applied explicitly so the process umask cannot
    change the resulting permissions.  The temporary is removed on every
    failed path, including failures from the final directory sync.
    """

    destination = Path(path)
    parent = destination.parent
    temporary: Path | None = None
    fd: int | None = None
    try:
        fd, temporary = _temporary_file(parent, destination.name)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
            fd = None
            os.chmod(temporary, mode)
            stream.write(text)
            _flush_file(stream)
        os.replace(temporary, destination)
        temporary = None
        sync_directory(parent)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        _remove_temporary(temporary)


def _copy_to_temporary(source: Path, temporary: Path) -> None:
    # copy2 carries mode and timestamps in addition to file contents.  The
    # temporary is then fsynced before it can become visible at the destination.
    shutil.copy2(source, temporary)
    with temporary.open("rb") as stream:
        _flush_file(stream)


def atomic_copy(source: Path, destination: Path) -> None:
    """Copy *source* to *destination* by atomically replacing its old name."""

    source_path = Path(source)
    destination_path = Path(destination)
    parent = destination_path.parent
    temporary: Path | None = None
    fd: int | None = None
    try:
        fd, temporary = _temporary_file(parent, destination_path.name)
        os.close(fd)
        fd = None
        _copy_to_temporary(source_path, temporary)
        os.replace(temporary, destination_path)
        temporary = None
        sync_directory(parent)
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        _remove_temporary(temporary)


def atomic_copy_if_absent(source: Path, destination: Path) -> bool:
    """Copy *source* only if *destination* does not already exist.

    A hard link from the fsynced temporary provides the no-overwrite
    compare-and-create operation.  The temporary is unlinked after the link;
    both namespace mutations are flushed together.
    """

    source_path = Path(source)
    destination_path = Path(destination)
    parent = destination_path.parent
    temporary: Path | None = None
    fd: int | None = None
    try:
        fd, temporary = _temporary_file(parent, destination_path.name)
        os.close(fd)
        fd = None
        _copy_to_temporary(source_path, temporary)
        try:
            os.link(temporary, destination_path)
        except FileExistsError:
            _remove_temporary(temporary)
            temporary = None
            return False
        temporary.unlink()
        temporary = None
        sync_directory(parent)
        return True
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        _remove_temporary(temporary)


__all__ = [
    "atomic_copy",
    "atomic_copy_if_absent",
    "atomic_write_text",
    "sync_directory",
]
