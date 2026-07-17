from __future__ import annotations

import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crossover_electron_bridge import atomic


class AtomicStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)

    def _temporary_artifacts(self, destination: Path) -> list[Path]:
        return list(destination.parent.glob(f".{destination.name}.*.tmp"))

    def test_atomic_write_text_writes_contents_mode_and_syncs_parent(self) -> None:
        destination = self.root / "state" / "save.json"
        destination.parent.mkdir()

        with mock.patch.object(atomic, "sync_directory") as sync:
            atomic.atomic_write_text(destination, '{"ok": true}', mode=0o640)

        self.assertEqual(destination.read_text(encoding="utf-8"), '{"ok": true}')
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o640)
        sync.assert_called_once_with(destination.parent)
        self.assertEqual(self._temporary_artifacts(destination), [])

    def test_atomic_write_text_replaces_existing_contents(self) -> None:
        destination = self.root / "state.txt"
        destination.write_text("old", encoding="utf-8")

        atomic.atomic_write_text(destination, "new")

        self.assertEqual(destination.read_text(encoding="utf-8"), "new")
        self.assertEqual(self._temporary_artifacts(destination), [])

    def test_atomic_copy_copies_contents_and_file_metadata(self) -> None:
        source = self.root / "source.bin"
        destination = self.root / "destination.bin"
        source.write_bytes(b"payload")
        os.chmod(source, 0o741)
        timestamp = 1_600_000_123_456_789_000
        os.utime(source, ns=(timestamp, timestamp))

        atomic.atomic_copy(source, destination)

        self.assertEqual(destination.read_bytes(), b"payload")
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o741)
        self.assertEqual(destination.stat().st_mtime_ns, timestamp)
        self.assertEqual(self._temporary_artifacts(destination), [])

    def test_atomic_copy_replaces_existing_destination(self) -> None:
        source = self.root / "source"
        destination = self.root / "destination"
        source.write_bytes(b"new")
        destination.write_bytes(b"old")

        atomic.atomic_copy(source, destination)

        self.assertEqual(destination.read_bytes(), b"new")
        self.assertEqual(self._temporary_artifacts(destination), [])

    def test_atomic_copy_if_absent_does_not_overwrite(self) -> None:
        source = self.root / "source"
        destination = self.root / "destination"
        source.write_bytes(b"source")
        destination.write_bytes(b"existing")

        with mock.patch.object(atomic, "sync_directory") as sync:
            result = atomic.atomic_copy_if_absent(source, destination)

        self.assertFalse(result)
        self.assertEqual(destination.read_bytes(), b"existing")
        sync.assert_not_called()
        self.assertEqual(self._temporary_artifacts(destination), [])

    def test_atomic_copy_if_absent_creates_once_and_syncs_parent(self) -> None:
        source = self.root / "source"
        destination = self.root / "destination"
        source.write_bytes(b"source")

        with mock.patch.object(atomic, "sync_directory") as sync:
            result = atomic.atomic_copy_if_absent(source, destination)

        self.assertTrue(result)
        self.assertEqual(destination.read_bytes(), b"source")
        sync.assert_called_once_with(destination.parent)
        self.assertEqual(self._temporary_artifacts(destination), [])

    def test_sync_directory_is_usable_on_real_directory(self) -> None:
        atomic.sync_directory(self.root)

    def test_write_cleanup_when_rename_fails(self) -> None:
        destination = self.root / "destination"
        with mock.patch.object(atomic.os, "replace", side_effect=OSError("rename failed")):
            with self.assertRaisesRegex(OSError, "rename failed"):
                atomic.atomic_write_text(destination, "payload")

        self.assertFalse(destination.exists())
        self.assertEqual(self._temporary_artifacts(destination), [])

    def test_copy_cleanup_when_copy_fails(self) -> None:
        source = self.root / "source"
        destination = self.root / "destination"
        source.write_bytes(b"source")
        with mock.patch.object(atomic.shutil, "copy2", side_effect=OSError("copy failed")):
            with self.assertRaisesRegex(OSError, "copy failed"):
                atomic.atomic_copy(source, destination)

        self.assertFalse(destination.exists())
        self.assertEqual(self._temporary_artifacts(destination), [])

    def test_copy_if_absent_cleanup_when_link_fails(self) -> None:
        source = self.root / "source"
        destination = self.root / "destination"
        source.write_bytes(b"source")
        with mock.patch.object(atomic.os, "link", side_effect=OSError("link failed")):
            with self.assertRaisesRegex(OSError, "link failed"):
                atomic.atomic_copy_if_absent(source, destination)

        self.assertFalse(destination.exists())
        self.assertEqual(self._temporary_artifacts(destination), [])

    def test_cleanup_when_directory_sync_fails_after_replace(self) -> None:
        destination = self.root / "destination"
        with mock.patch.object(atomic, "sync_directory", side_effect=OSError("sync failed")):
            with self.assertRaisesRegex(OSError, "sync failed"):
                atomic.atomic_write_text(destination, "payload")

        self.assertEqual(destination.read_text(encoding="utf-8"), "payload")
        self.assertEqual(self._temporary_artifacts(destination), [])


if __name__ == "__main__":
    unittest.main()
