from __future__ import annotations

import hashlib
import json
from pathlib import Path
import stat
import tempfile
import unittest
from unittest import mock

from crossover_electron_bridge.errors import OperationalError
import crossover_electron_bridge.patching as patching_module
from crossover_electron_bridge.patching import PatchManager, PatchResult
from crossover_electron_bridge.profiles import (
    GameProfile,
    RendererConfig,
    SteamBuild,
    SteamConfig,
)


class FakeAsarTool:
    """Deterministic ASAR substitute; no process or npx invocation."""

    def __init__(self, source: bytes):
        self.source = source
        self.extract_calls = 0
        self.pack_calls = 0

    def extract(self, archive: Path, destination: Path) -> None:
        self.extract_calls += 1
        destination.mkdir(parents=True)
        (destination / "electron").mkdir()
        (destination / "electron/main.cjs").write_bytes(archive.read_bytes())

    def pack(self, source: Path, archive: Path) -> None:
        self.pack_calls += 1
        entry = (source / "electron/main.cjs").read_bytes()
        archive.write_bytes(b"FAKE-ASAR\0" + entry)


class FakeAdapter:
    revision = "CROSSOVER_BROWSER_GAMES_TEST_V1"

    def apply(self, extracted_root: Path, profile: GameProfile, resources) -> None:
        entry = extracted_root / profile.renderer.entry
        entry.write_bytes(entry.read_bytes() + self.revision.encode("ascii"))


class PatchManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.original = b"deterministic original archive bytes\n"
        digest = hashlib.sha256(self.original).hexdigest()
        self.profile = GameProfile(
            schema_version=1,
            id="fixture-game",
            display_name="Fixture Game",
            mode="steam-electron",
            bottle="Steam",
            steam=SteamConfig(
                app_id=123,
                install_dir="Fixture Game",
                supported_builds=(SteamBuild("build-1", digest, len(self.original)),),
            ),
            renderer=RendererConfig(
                assets="resources/app/dist",
                bind_host="127.0.0.1",
                browser_host="127.0.0.1",
                port=48760,
                archive="resources/app.asar",
                entry="electron/main.cjs",
            ),
            adapter="fractured_realms",
            state_key="fixture-game",
        )
        bottle = self.home / "Library/Application Support/CrossOver/Bottles/Steam"
        self.manifest = bottle / "drive_c/Program Files (x86)/Steam/steamapps/appmanifest_123.acf"
        self.archive = bottle / "drive_c/Program Files (x86)/Steam/steamapps/common/Fixture Game/resources/app.asar"
        self.manifest.parent.mkdir(parents=True)
        self.archive.parent.mkdir(parents=True)
        self.manifest.write_text(
            '"AppState"\n{\n"appid" "123"\n"name" "Fixture Game"\n"installdir" "Fixture Game"\n"buildid" "build-1"\n}\n',
            encoding="utf-8",
        )
        self.archive.write_bytes(self.original)
        self.tool = FakeAsarTool(self.original)
        self.adapter = FakeAdapter()
        self.manager = PatchManager(
            home=self.home,
            asar_tool=self.tool,
            clock=lambda: "2026-01-02T03:04:05+00:00",
            revision_resolver=lambda profile: self.adapter.revision,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _snapshot(self) -> dict[Path, bytes]:
        return {
            path.relative_to(self.home): path.read_bytes()
            for path in self.home.rglob("*")
            if path.is_file()
        }

    def _assert_snapshot_unchanged(
        self,
        before: dict[Path, bytes],
        changed: dict[Path, bytes] | None = None,
    ) -> None:
        changed = changed or {}
        after = self._snapshot()
        for relative, contents in before.items():
            expected = changed.get(relative, contents)
            self.assertEqual(after.get(relative), expected, relative)

    def test_clean_patch_writes_v2_metadata_and_immutable_backup(self) -> None:
        result = self.manager.patch(self.profile, self.adapter)

        self.assertEqual(result, PatchResult(True, self.archive, self.home / ".local/state/crossover-electron-bridge/fixture-game/metadata.json"))
        self.assertTrue(result.archive.read_bytes().endswith(self.adapter.revision.encode()))
        metadata = json.loads(result.metadata.read_text(encoding="utf-8"))
        self.assertEqual(
            set(metadata),
            {
                "metadata_version", "profile_id", "profile_revision", "marker",
                "steam_build_id", "timestamp", "original", "patched", "backup",
            },
        )
        self.assertEqual(metadata["metadata_version"], 2)
        self.assertEqual(metadata["profile_id"], "fixture-game")
        self.assertEqual(metadata["backup"]["path"], f"backups/app.asar-{metadata['original']['sha256']}.original")
        self.assertEqual(stat.S_IMODE(result.metadata.stat().st_mode), 0o600)
        self.assertEqual(
            (result.metadata.parent / metadata["backup"]["path"]).read_bytes(), self.original
        )

    def test_clock_failure_before_replacement_leaves_fixture_bytes_unchanged(self) -> None:
        before = self._snapshot()

        def fail_clock() -> str:
            raise OperationalError("clock unavailable")

        manager = PatchManager(
            home=self.home,
            asar_tool=self.tool,
            clock=fail_clock,
            revision_resolver=lambda profile: self.adapter.revision,
        )
        with self.assertRaises(OperationalError):
            manager.patch(self.profile, self.adapter)

        self._assert_snapshot_unchanged(before)
        self.assertEqual(self.archive.read_bytes(), self.original)
        self.assertFalse(manager.metadata_path(self.profile).exists())

    def test_metadata_write_failure_after_replacement_rolls_back_and_keeps_backup(self) -> None:
        before = self._snapshot()
        installed = False

        def observe_replacement(path: Path) -> None:
            nonlocal installed
            installed = True
            self.assertEqual(path, self.archive)
            self.assertTrue(self.archive.read_bytes().endswith(self.adapter.revision.encode()))

        def fail_metadata(path: Path, text: str, mode: int = 0o600) -> None:
            self.assertEqual(Path(path), self.manager.metadata_path(self.profile))
            self.assertTrue(installed)
            raise OSError("metadata device full")

        manager = PatchManager(
            home=self.home,
            asar_tool=self.tool,
            clock=lambda: "2026-01-02T03:04:05+00:00",
            hooks={"after_replace": observe_replacement},
            revision_resolver=lambda profile: self.adapter.revision,
        )
        with mock.patch(
            "crossover_electron_bridge.patching.atomic_write_text",
            side_effect=fail_metadata,
        ):
            with self.assertRaises(OperationalError):
                manager.patch(self.profile, self.adapter)

        self._assert_snapshot_unchanged(before)
        self.assertEqual(self.archive.read_bytes(), self.original)
        backup = (
            manager.state_dir(self.profile)
            / "backups"
            / f"app.asar-{hashlib.sha256(self.original).hexdigest()}.original"
        )
        self.assertEqual(backup.read_bytes(), self.original)
        self.assertFalse(manager.metadata_path(self.profile).exists())

    def test_metadata_failure_refuses_rollback_after_concurrent_live_change(self) -> None:
        changed = b"third-party archive installed after replacement\n"
        before = self._snapshot()
        installed = False

        def fail_metadata(path: Path, text: str, mode: int = 0o600) -> None:
            nonlocal installed
            installed = self.archive.read_bytes().endswith(self.adapter.revision.encode())
            self.assertTrue(installed)
            self.archive.write_bytes(changed)
            raise OSError("metadata device full")

        manager = PatchManager(
            home=self.home,
            asar_tool=self.tool,
            clock=lambda: "2026-01-02T03:04:05+00:00",
            revision_resolver=lambda profile: self.adapter.revision,
        )
        with mock.patch(
            "crossover_electron_bridge.patching.atomic_write_text",
            side_effect=fail_metadata,
        ):
            with self.assertRaises(OperationalError):
                manager.patch(self.profile, self.adapter)

        self.assertTrue(installed)
        self._assert_snapshot_unchanged(
            before,
            {self.archive.relative_to(self.home): changed},
        )
        self.assertEqual(self.archive.read_bytes(), changed)
        backup = (
            manager.state_dir(self.profile)
            / "backups"
            / f"app.asar-{hashlib.sha256(self.original).hexdigest()}.original"
        )
        self.assertEqual(backup.read_bytes(), self.original)

    def test_atomic_copy_failure_after_replacement_rolls_back_without_metadata(self) -> None:
        before = self._snapshot()
        backup = (
            self.manager.state_dir(self.profile)
            / "backups"
            / f"app.asar-{hashlib.sha256(self.original).hexdigest()}.original"
        )
        packed = b"FAKE-ASAR\0" + self.original + self.adapter.revision.encode("ascii")
        real_atomic_copy = patching_module.atomic_copy
        replaced = False

        def fail_after_replacement(source: Path, destination: Path) -> None:
            nonlocal replaced
            real_atomic_copy(source, destination)
            if destination == self.archive and not replaced:
                replaced = True
                self.assertEqual(self.archive.read_bytes(), packed)
                raise OSError("replacement completed, then copy reported failure")

        with mock.patch.object(
            patching_module,
            "atomic_copy",
            side_effect=fail_after_replacement,
        ):
            with self.assertRaisesRegex(OperationalError, "verified original restored"):
                self.manager.patch(self.profile, self.adapter)

        self.assertTrue(replaced)
        expected = dict(before)
        expected[backup.relative_to(self.home)] = self.original
        self.assertEqual(self._snapshot(), expected)
        self.assertEqual(self.archive.read_bytes(), self.original)
        self.assertFalse(self.manager.metadata_path(self.profile).exists())

    def test_installed_fingerprint_failure_rolls_back_when_live_remains_packed(self) -> None:
        before = self._snapshot()
        backup = (
            self.manager.state_dir(self.profile)
            / "backups"
            / f"app.asar-{hashlib.sha256(self.original).hexdigest()}.original"
        )
        packed = b"FAKE-ASAR\0" + self.original + self.adapter.revision.encode("ascii")
        real_fingerprint = patching_module.fingerprint
        faulted = False

        def fail_first_installed_read(path: Path, marker: bytes | None = None):
            nonlocal faulted
            if path == self.archive and not faulted and path.read_bytes() == packed:
                faulted = True
                raise OSError("installed fingerprint unavailable")
            return real_fingerprint(path, marker)

        with mock.patch.object(
            patching_module,
            "fingerprint",
            side_effect=fail_first_installed_read,
        ):
            with self.assertRaisesRegex(OperationalError, "verified original restored"):
                self.manager.patch(self.profile, self.adapter)

        self.assertTrue(faulted)
        expected = dict(before)
        expected[backup.relative_to(self.home)] = self.original
        self.assertEqual(self._snapshot(), expected)
        self.assertEqual(self.archive.read_bytes(), self.original)
        self.assertFalse(self.manager.metadata_path(self.profile).exists())

    def test_installed_fingerprint_failure_preserves_third_party_mutation(self) -> None:
        before = self._snapshot()
        changed = b"third-party archive installed during fingerprint failure\n"
        backup = (
            self.manager.state_dir(self.profile)
            / "backups"
            / f"app.asar-{hashlib.sha256(self.original).hexdigest()}.original"
        )
        packed = b"FAKE-ASAR\0" + self.original + self.adapter.revision.encode("ascii")
        real_fingerprint = patching_module.fingerprint
        faulted = False

        def fail_after_third_party_mutation(path: Path, marker: bytes | None = None):
            nonlocal faulted
            if path == self.archive and not faulted and path.read_bytes() == packed:
                faulted = True
                self.archive.write_bytes(changed)
                raise OSError("installed fingerprint unavailable")
            return real_fingerprint(path, marker)

        with mock.patch.object(
            patching_module,
            "fingerprint",
            side_effect=fail_after_third_party_mutation,
        ):
            with self.assertRaisesRegex(OperationalError, "rollback was not attempted"):
                self.manager.patch(self.profile, self.adapter)

        self.assertTrue(faulted)
        expected = dict(before)
        expected[backup.relative_to(self.home)] = self.original
        expected[self.archive.relative_to(self.home)] = changed
        self.assertEqual(self._snapshot(), expected)
        self.assertEqual(self.archive.read_bytes(), changed)
        self.assertFalse(self.manager.metadata_path(self.profile).exists())

    def test_repeat_patch_is_idempotent_after_full_state_verification(self) -> None:
        first = self.manager.patch(self.profile, self.adapter)
        extract_calls, pack_calls = self.tool.extract_calls, self.tool.pack_calls

        second = self.manager.patch(self.profile, self.adapter)

        self.assertFalse(second.changed)
        self.assertEqual(second.archive, first.archive)
        self.assertEqual((self.tool.extract_calls, self.tool.pack_calls), (extract_calls, pack_calls))

    def test_idempotent_archive_change_during_late_verification_refuses(self) -> None:
        self.manager.patch(self.profile, self.adapter)
        before = self._snapshot()
        changed = b"third-party archive change during idempotence check\n"
        current_build = self.manager._current_build_and_manifest
        calls = 0

        def change_archive_late(profile: GameProfile, resolved) -> str:
            nonlocal calls
            calls += 1
            build_id = current_build(profile, resolved)
            if calls == 2:
                self.archive.write_bytes(changed)
            return build_id

        with mock.patch.object(
            self.manager,
            "_current_build_and_manifest",
            side_effect=change_archive_late,
        ):
            with self.assertRaises(OperationalError):
                self.manager.patch(self.profile, self.adapter)

        self.assertEqual(calls, 2)
        self._assert_snapshot_unchanged(
            before,
            {self.archive.relative_to(self.home): changed},
        )
        self.assertEqual(self.archive.read_bytes(), changed)

    def test_idempotent_build_change_during_late_verification_refuses(self) -> None:
        self.manager.patch(self.profile, self.adapter)
        before = self._snapshot()
        changed_manifest = (
            '"AppState"\n{\n"appid" "123"\n"name" "Fixture Game"\n'
            '"installdir" "Fixture Game"\n"buildid" "build-2"\n}\n'
        ).encode()
        current_build = self.manager._current_build_and_manifest
        calls = 0

        def change_build_late(profile: GameProfile, resolved) -> str:
            nonlocal calls
            calls += 1
            if calls == 2:
                self.manifest.write_bytes(changed_manifest)
            return current_build(profile, resolved)

        with mock.patch.object(
            self.manager,
            "_current_build_and_manifest",
            side_effect=change_build_late,
        ):
            with self.assertRaises(OperationalError):
                self.manager.patch(self.profile, self.adapter)

        self.assertEqual(calls, 2)
        self._assert_snapshot_unchanged(
            before,
            {self.manifest.relative_to(self.home): changed_manifest},
        )
        self.assertEqual(self.archive.read_bytes(), before[self.archive.relative_to(self.home)])

    def test_concurrent_live_change_is_refused_without_replacement(self) -> None:
        changed = b"steam update arrived while preparing\n"
        before = self._snapshot()

        def race(path: Path) -> None:
            path.write_bytes(changed)

        manager = PatchManager(
            home=self.home,
            asar_tool=self.tool,
            hooks={"before_replace": race},
            revision_resolver=lambda profile: self.adapter.revision,
        )
        with self.assertRaisesRegex(OperationalError, "changed while preparing"):
            manager.patch(self.profile, self.adapter)
        self.assertEqual(self.archive.read_bytes(), changed)
        self._assert_snapshot_unchanged(
            before,
            {self.archive.relative_to(self.home): changed},
        )
        self.assertFalse((self.home / ".local/state/crossover-electron-bridge/fixture-game/metadata.json").exists())

    def test_unsupported_build_and_original_mismatch_write_nothing(self) -> None:
        before = self._snapshot()
        self.manifest.write_text(
            '"AppState"\n{\n"appid" "123"\n"name" "Fixture Game"\n"installdir" "Fixture Game"\n"buildid" "new-build"\n}\n',
            encoding="utf-8",
        )
        before_refusal = self._snapshot()
        with self.assertRaisesRegex(OperationalError, r"Unsupported Steam build new-build for fixture-game"):
            self.manager.patch(self.profile, self.adapter)
        self.assertEqual(self._snapshot(), before_refusal)
        self.assertEqual(self.archive.read_bytes(), self.original)

        # The manifest is an input fixture changed by this test; restore it to
        # prove an original-fingerprint refusal does not write any other file.
        self.manifest.write_text(
            '"AppState"\n{\n"appid" "123"\n"name" "Fixture Game"\n"installdir" "Fixture Game"\n"buildid" "build-1"\n}\n',
            encoding="utf-8",
        )
        self.archive.write_bytes(b"not the supported original")
        before_refusal = self._snapshot()
        self.assertNotEqual(before[self.archive.relative_to(self.home)], self.archive.read_bytes())
        with self.assertRaisesRegex(OperationalError, r"Unsupported Steam build build-1 for fixture-game"):
            self.manager.patch(self.profile, self.adapter)
        self.assertEqual(self._snapshot(), before_refusal)
        self.assertFalse((self.home / ".local/state/crossover-electron-bridge").exists())

    def test_marker_without_metadata_refuses_without_repacking(self) -> None:
        self.archive.write_bytes(self.original + self.adapter.revision.encode())
        before = self._snapshot()

        with self.assertRaisesRegex(OperationalError, "recovery state cannot be verified"):
            self.manager.patch(self.profile, self.adapter)

        self.assertEqual(self._snapshot(), before)
        self.assertEqual(self.tool.extract_calls, 0)
        self.assertEqual(self.tool.pack_calls, 0)

    def test_restore_success_retains_metadata_and_backup(self) -> None:
        patched = self.manager.patch(self.profile, self.adapter)
        backup = patched.metadata.parent / "backups" / f"app.asar-{hashlib.sha256(self.original).hexdigest()}.original"

        self.manager.restore(self.profile)

        self.assertEqual(self.archive.read_bytes(), self.original)
        self.assertTrue(patched.metadata.exists())
        self.assertEqual(backup.read_bytes(), self.original)

    def test_restore_refuses_after_retained_backup_mutation_before_replacement(self) -> None:
        patched = self.manager.patch(self.profile, self.adapter)
        before = self._snapshot()
        metadata = json.loads(patched.metadata.read_text(encoding="utf-8"))
        backup = patched.metadata.parent / metadata["backup"]["path"]
        mutated_backup = b"third-party mutation of immutable backup\n"

        def mutate_backup(path: Path) -> None:
            self.assertEqual(path, self.archive)
            backup.write_bytes(mutated_backup)

        manager = PatchManager(
            home=self.home,
            asar_tool=self.tool,
            revision_resolver=lambda profile: self.adapter.revision,
            hooks={"before_restore_replace": mutate_backup},
        )
        with self.assertRaisesRegex(OperationalError, "backup"):
            manager.restore(self.profile)

        self._assert_snapshot_unchanged(
            before,
            {backup.relative_to(self.home): mutated_backup},
        )
        self.assertEqual(self.archive.read_bytes(), before[self.archive.relative_to(self.home)])
        self.assertEqual(patched.metadata.read_bytes(), before[patched.metadata.relative_to(self.home)])
        self.assertEqual(backup.read_bytes(), mutated_backup)

    def test_patch_rolls_back_after_success_boundary_backup_corruption(self) -> None:
        mutated_backup = b"third-party corruption retained for diagnosis\n"
        installed = False
        backup = (
            self.manager.state_dir(self.profile)
            / "backups"
            / f"app.asar-{hashlib.sha256(self.original).hexdigest()}.original"
        )

        def corrupt_backup(path: Path) -> None:
            nonlocal installed
            self.assertEqual(path, self.manager.metadata_path(self.profile))
            installed = self.archive.read_bytes().endswith(self.adapter.revision.encode())
            self.assertTrue(installed)
            backup.write_bytes(mutated_backup)

        manager = PatchManager(
            home=self.home,
            asar_tool=self.tool,
            clock=lambda: "2026-01-02T03:04:05+00:00",
            hooks={"after_metadata_commit": corrupt_backup},
            revision_resolver=lambda profile: self.adapter.revision,
        )
        with self.assertRaisesRegex(OperationalError, "verified original restored"):
            manager.patch(self.profile, self.adapter)

        self.assertTrue(installed)
        self.assertEqual(self.archive.read_bytes(), self.original)
        self.assertEqual(backup.read_bytes(), mutated_backup)
        metadata_path = manager.metadata_path(self.profile)
        self.assertTrue(metadata_path.exists())
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        self.assertEqual(metadata["backup"]["sha256"], hashlib.sha256(self.original).hexdigest())
        self.assertNotEqual(
            hashlib.sha256(backup.read_bytes()).hexdigest(), metadata["backup"]["sha256"]
        )

    def test_restore_refuses_when_revision_resolver_is_unavailable(self) -> None:
        patched = self.manager.patch(self.profile, self.adapter)
        before = self._snapshot()

        def unavailable(profile: GameProfile) -> str:
            raise OperationalError("revision resolver unavailable")

        manager = PatchManager(
            home=self.home,
            asar_tool=self.tool,
            revision_resolver=unavailable,
        )
        with self.assertRaises(OperationalError):
            manager.restore(self.profile)

        self._assert_snapshot_unchanged(before)
        self.assertEqual(self.archive.read_bytes(), before[self.archive.relative_to(self.home)])
        self.assertEqual(patched.metadata.read_bytes(), before[patched.metadata.relative_to(self.home)])

    def test_restore_refuses_when_code_revision_does_not_match(self) -> None:
        patched = self.manager.patch(self.profile, self.adapter)
        before = self._snapshot()
        manager = PatchManager(
            home=self.home,
            asar_tool=self.tool,
            revision_resolver=lambda profile: "CROSSOVER_BROWSER_GAMES_OTHER_REVISION",
        )

        with self.assertRaises(OperationalError):
            manager.restore(self.profile)

        self._assert_snapshot_unchanged(before)
        self.assertEqual(self.archive.read_bytes(), before[self.archive.relative_to(self.home)])
        self.assertEqual(patched.metadata.read_bytes(), before[patched.metadata.relative_to(self.home)])

    def test_restore_refuses_stale_build_without_changing_archive_or_state(self) -> None:
        patched = self.manager.patch(self.profile, self.adapter)
        before_archive = self.archive.read_bytes()
        before_metadata = patched.metadata.read_bytes()
        self.manifest.write_text(
            '"AppState"\n{\n"appid" "123"\n"name" "Fixture Game"\n"installdir" "Fixture Game"\n"buildid" "build-2"\n}\n',
            encoding="utf-8",
        )
        before_refusal = self._snapshot()
        with self.assertRaises(OperationalError):
            self.manager.restore(self.profile)
        self.assertEqual(self._snapshot(), before_refusal)
        self.assertEqual(self.archive.read_bytes(), before_archive)
        self.assertEqual(patched.metadata.read_bytes(), before_metadata)


if __name__ == "__main__":
    unittest.main()
