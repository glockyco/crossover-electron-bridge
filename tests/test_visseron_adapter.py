from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

from crossover_electron_bridge.adapters.visseron_idle import VisseronAdapter
from crossover_electron_bridge.browser import ApiRequest, ApiResponse
from crossover_electron_bridge.profiles import GameProfile, RendererConfig, SteamBuild, SteamConfig


class VisseronAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name)
        self.now_value = 1000.0
        self.profile = GameProfile(
            schema_version=1,
            id="visseron-idle",
            display_name="Visseron Idle",
            mode="browser-only",
            bottle="Steam",
            steam=SteamConfig(4601670, "Visseron Idle", (SteamBuild("24193581"),)),
            renderer=RendererConfig("resources/app/dist", "127.0.0.1", "localhost", 8765),
            adapter="visseron_idle",
            state_key="visseron-idle",
            adapter_config=MappingProxyType(
                {
                    "save_dir": "~/.visseron-idle",
                    "wine_state_path": "users/crossover/AppData/Roaming/visseron-idle",
                    "max_request_bytes": 4 * 1024 * 1024,
                    "max_backups": 10,
                    "backup_interval_seconds": 300,
                    "backup_slots": (1, 2, 3),
                }
            ),
        )
        self.adapter = VisseronAdapter(
            self.profile, home=self.home, now=lambda: self.now_value
        )
        self.adapter.before_start()
        self.wine = (
            self.home
            / "Library/Application Support/CrossOver/Bottles/Steam/drive_c/users/crossover/AppData/Roaming/visseron-idle"
        )
        self.save = self.home / ".visseron-idle/save-data.json"
        self.addCleanup(self.temp.cleanup)

    def _request(
        self, method: str, path: str, body: bytes = b"", **headers: str
    ) -> ApiResponse | None:
        return self.adapter.handle_api(ApiRequest(method, path, headers, body))

    def test_first_run_imports_valid_wine_save_atomically(self) -> None:
        data = '{"savedAt":123,"totalPlayTime":9}'
        self.wine.mkdir(parents=True)
        (self.wine / "save-data.json").write_text(data, encoding="utf-8")

        self.assertEqual(self.adapter.load_save(), data)
        self.assertEqual(self.save.read_text(encoding="utf-8"), data)
        self.assertEqual(self.save.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.adapter.load_save(), data)

    def test_malformed_wine_import_does_not_create_save(self) -> None:
        self.wine.mkdir(parents=True)
        (self.wine / "save-data.json").write_text('{"unterminated"', encoding="utf-8")
        self.assertIsNone(self.adapter.load_save())
        self.assertFalse(self.save.exists())

    def test_save_round_trip_and_permissions(self) -> None:
        payload = '{"savedAt":1,"totalPlayTime":2}'
        response = self._request(
            "POST", "/api/save", payload.encode(), **{"Content-Type": "application/json"}
        )
        self.assertEqual(response, ApiResponse(200, {"success": True}))
        loaded = self._request("GET", "/api/load")
        self.assertEqual(loaded, ApiResponse(200, {"save": payload}))
        self.assertEqual(self.save.read_text(encoding="utf-8"), payload)
        self.assertEqual(self.save.stat().st_mode & 0o777, 0o600)

    def test_malformed_json_refuses_without_mutating_save(self) -> None:
        original = '{"value":"before"}'
        self.adapter.write_save(original)
        snapshot = self.save.read_bytes()
        response = self._request(
            "POST", "/api/save", b'{"value":', **{"Content-Type": "application/json"}
        )
        self.assertEqual(
            response,
            ApiResponse(
                400,
                {"success": False, "error": "Request body must be valid JSON"},
            ),
        )
        self.assertEqual(self.save.read_bytes(), snapshot)
        manual = self.home / ".visseron-idle/save-manual.json"
        self.adapter.write_manual_save('{"manual":true}')
        manual_snapshot = manual.read_bytes()
        response = self._request(
            "POST",
            "/api/manual-save",
            b"not-json",
            **{"Content-Type": "application/json"},
        )
        self.assertEqual(response.status, 400)
        self.assertEqual(manual.read_bytes(), manual_snapshot)

    def test_prior_generation_and_five_minute_interval(self) -> None:
        first = '{"generation":1}'
        second = '{"generation":2}'
        third = '{"generation":3}'
        self.adapter.write_save(first)
        self.now_value += 301
        self.adapter.write_save(second)
        backups = sorted(self.adapter.backup_dir.glob("save-*.json"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), first)
        self.now_value += 100
        self.adapter.write_save(third)
        self.assertEqual(len(list(self.adapter.backup_dir.glob("save-*.json"))), 1)

    def test_rolling_backup_pruning(self) -> None:
        config = dict(self.profile.adapter_config)
        config.update({"max_backups": 3, "backup_interval_seconds": 0})
        profile = GameProfile(
            schema_version=self.profile.schema_version,
            id=self.profile.id,
            display_name=self.profile.display_name,
            mode=self.profile.mode,
            bottle=self.profile.bottle,
            steam=self.profile.steam,
            renderer=self.profile.renderer,
            adapter=self.profile.adapter,
            state_key=self.profile.state_key,
            adapter_config=config,
        )
        adapter = VisseronAdapter(profile, home=self.home, now=lambda: self.now_value)
        adapter.before_start()
        adapter.write_save('{"generation":0}')
        for generation in range(1, 7):
            self.now_value += 1
            adapter.write_save(json.dumps({"generation": generation}))
        backups = sorted(adapter.backup_dir.glob("save-*.json"))
        self.assertEqual(len(backups), 3)
        self.assertEqual(
            [json.loads(path.read_text(encoding="utf-8"))["generation"] for path in backups],
            [3, 4, 5],
        )

    def test_wine_slots_metadata_and_manual_save(self) -> None:
        self.wine.mkdir(parents=True)
        for slot in (1, 2, 3):
            (self.wine / f"save-backup-{slot}.json").write_text(
                json.dumps({"savedAt": slot * 10, "totalPlayTime": slot * 100}),
                encoding="utf-8",
            )
        response = self._request("GET", "/api/backups")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body[0], {"slot": "1", "label": "Backup 1", "savedAt": 10, "totalPlayTime": 100})
        manual = self._request(
            "POST",
            "/api/manual-save",
            b"{\"manual\":true}",
            **{"Content-Type": "application/json"},
        )
        self.assertEqual(manual, ApiResponse(200, {"success": True}))
        self.assertEqual(
            (self.home / ".visseron-idle/save-manual.json").read_text(),
            '{"manual":true}',
        )

    def test_restore_slot_delete_and_traversal_safe_slot(self) -> None:
        self.wine.mkdir(parents=True)
        backup = '{"restored":true}'
        (self.wine / "save-backup-2.json").write_text(backup, encoding="utf-8")
        self.adapter.write_save('{"old":true}')
        restored = self._request("POST", "/api/restore-backup", b"2")
        self.assertEqual(restored, ApiResponse(200, {"success": True}))
        self.assertEqual(self.save.read_text(), backup)
        self.assertEqual(
            self._request("POST", "/api/restore-backup", b"../2").body,
            {"success": False, "error": "Invalid backup slot"},
        )
        self.assertEqual(self._request("POST", "/api/delete"), ApiResponse(200, {"success": True}))
        self.assertFalse(self.save.exists())

    def test_exact_routes_methods_media_and_unknown(self) -> None:
        self.assertIsNone(self._request("GET", "/api/not-a-route"))
        wrong = self._request("POST", "/api/load")
        self.assertEqual(wrong.status, 405)
        wrong_media = self._request("POST", "/api/save", b"{}", **{"Content-Type": "text/plain"})
        self.assertEqual(wrong_media.status, 415)
        self.assertEqual(
            self._request("POST", "/api/save", b"{}", **{"Content-Type": "application/json"}),
            ApiResponse(200, {"success": True}),
        )

    def test_bridge_has_exact_wire_methods_and_unavailable_services(self) -> None:
        bridge = self.adapter.bridge_script("safe-token")
        self.assertIn('const crossoverToken = "safe-token";', bridge)
        for name in (
            "save", "load", "deleteSave", "getSavePath", "getLogPath", "onClosing",
            "confirmClose", "enableAutoSave", "loadBackups", "restoreBackup",
            "requestCloseRotation", "manualSave", "steam", "avatar",
        ):
            self.assertIn(name, bridge)
        self.assertIn("isAvailable: async () => false", bridge)
        self.assertIn("error: 'Not available in browser'", bridge)
        self.assertIn("'/api/restore-backup'", bridge)

    def test_mode_and_permission_behavior(self) -> None:
        self.assertEqual(self.profile.mode, "browser-only")
        with self.assertRaisesRegex(ValueError, "browser-only"):
            VisseronAdapter(
                replace(self.profile, mode="steam-electron"), home=self.home
            )
        self.adapter.write_save("null")
        self.adapter.write_manual_save("null")
        self.assertEqual(self.save.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.adapter.manual_save_file.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
