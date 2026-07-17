from __future__ import annotations

import hashlib
import http.server
import json
import shutil
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from crossover_electron_bridge.doctor import run_doctor
from crossover_electron_bridge.profiles import (
    GameProfile,
    RendererConfig,
    SteamBuild,
    SteamConfig,
)


class _HealthHandler(http.server.BaseHTTPRequestHandler):
    payload: dict[str, object] = {}

    def do_GET(self) -> None:
        body = json.dumps(self.payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args: object) -> None:
        return None


class DoctorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.home)
        self.wine = self.home / "wine"
        self.wine.write_text("wine", encoding="utf-8")
        self.bottle = self.home / "Library/Application Support/CrossOver/Bottles/Steam"
        self.steam_dir = self.bottle / "drive_c/Program Files (x86)/Steam"
        self.steam_dir.mkdir(parents=True)
        (self.steam_dir / "Steam.exe").write_bytes(b"steam")

    def _profile(
        self,
        *,
        mode: str = "browser-only",
        app_id: int = 1234,
        install_dir: str = "Example Game",
        build_id: str = "1001",
        archive: str | None = None,
        entry: str | None = None,
        assets: str = "resources/app/dist",
        create_assets: bool = True,
        adapter: str = "static",
        state_key: str = "example",
        browser_host: str = "127.0.0.1",
        port: int = 1,
        archive_sha256: str | None = None,
        archive_size: int | None = None,
    ) -> GameProfile:
        install = self.steam_dir / "steamapps/common" / install_dir
        install.mkdir(parents=True, exist_ok=True)
        if create_assets:
            (install / assets).mkdir(parents=True, exist_ok=True)
        manifest = self.steam_dir / "steamapps" / f"appmanifest_{app_id}.acf"
        manifest.write_text(
            "\n".join(
                [
                    '"AppState"',
                    "{",
                    f'\t"appid"\t"{app_id}"',
                    '\t"name"\t"Example Game"',
                    f'\t"installdir"\t"{install_dir}"',
                    f'\t"buildid"\t"{build_id}"',
                    "}",
                ]
            ),
            encoding="utf-8",
        )
        steam_build = SteamBuild(build_id, archive_sha256, archive_size)
        return GameProfile(
            schema_version=1,
            id="example",
            display_name="Example Game",
            mode=mode,  # type: ignore[arg-type]
            bottle="Steam",
            steam=SteamConfig(app_id, install_dir, (steam_build,)),
            renderer=RendererConfig(assets, "127.0.0.1", browser_host, port, archive, entry),
            adapter=adapter,
            state_key=state_key,
        )

    def _checks(self, profile: GameProfile) -> dict[str, object]:
        with mock.patch("crossover_electron_bridge.doctor.Path.home", return_value=self.home), mock.patch(
            "crossover_electron_bridge.doctor.crossover_wine", return_value=self.wine
        ), mock.patch("crossover_electron_bridge.doctor.platform.system", return_value="Darwin"), mock.patch(
            "crossover_electron_bridge.doctor.shutil.which", return_value="/usr/local/bin/npx"
        ):
            return {check.name: check for check in run_doctor(profile)}

    def test_browser_unlisted_build_is_warning(self) -> None:
        profile = self._profile(build_id="9999")
        profile = replace(
            profile,
            steam=replace(
                profile.steam,
                supported_builds=(SteamBuild("1001"),),
            ),
        )
        checks = self._checks(profile)
        self.assertEqual(checks["steam-build"].level, "WARN")
        self.assertIn("browser-only", checks["steam-build"].detail)
        self.assertFalse(any(check.level == "FAIL" for check in checks.values()))

    def test_steam_archive_fingerprint_mismatch_is_failure(self) -> None:
        original = b"known original"
        digest = hashlib.sha256(original).hexdigest()
        profile = self._profile(
            mode="steam-electron",
            build_id="2002",
            archive="resources/app.asar",
            entry="electron/main.cjs",
            adapter="fractured_realms",
            archive_sha256=digest,
            archive_size=len(original),
            create_assets=False,
        )
        archive = self.steam_dir / "steamapps/common/Example Game/resources/app.asar"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b"unexpected archive")
        checks = self._checks(profile)
        self.assertEqual(checks["steam-build"].level, "PASS")
        self.assertEqual(checks["archive"].level, "FAIL")
        self.assertIn("fingerprint mismatch", checks["archive"].detail)
        self.assertNotEqual(checks["assets"].level, "PASS")

    def test_steam_original_archive_is_valid(self) -> None:
        original = b"known original"
        digest = hashlib.sha256(original).hexdigest()
        profile = self._profile(
            mode="steam-electron",
            build_id="2002",
            archive="resources/app.asar",
            entry="electron/main.cjs",
            adapter="fractured_realms",
            archive_sha256=digest,
            archive_size=len(original),
            create_assets=False,
        )
        archive = self.steam_dir / "steamapps/common/Example Game/resources/app.asar"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(original)
        checks = self._checks(profile)
        self.assertEqual(checks["archive"].level, "PASS")
        self.assertEqual(checks["assets"].level, "PASS")
        self.assertIn("original fingerprint", checks["archive"].detail)

    def test_steam_patched_archive_requires_valid_metadata_and_backup(self) -> None:
        original = b"known original"
        revision = "CROSSOVER_BROWSER_GAMES_FRACTURED_REALMS_V1"
        patched = original + revision.encode("utf-8")
        original_digest = hashlib.sha256(original).hexdigest()
        patched_digest = hashlib.sha256(patched).hexdigest()
        profile = self._profile(
            mode="steam-electron",
            build_id="2002",
            archive="resources/app.asar",
            entry="electron/main.cjs",
            adapter="fractured_realms",
            state_key="example-main",
            archive_sha256=original_digest,
            archive_size=len(original),
            create_assets=False,
        )
        archive = self.steam_dir / "steamapps/common/Example Game/resources/app.asar"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(patched)
        state = self.home / ".local/state/crossover-electron-bridge/example-main"
        backup = state / "backups" / f"app.asar-{original_digest}.original"
        backup.parent.mkdir(parents=True)
        backup.write_bytes(original)
        (state / "metadata.json").write_text(
            json.dumps(
                {
                    "metadata_version": 2,
                    "profile_id": "example",
                    "profile_revision": revision,
                    "marker": revision,
                    "steam_build_id": "2002",
                    "timestamp": "2026-01-01T00:00:00+00:00",
                    "original": {"sha256": original_digest, "size": len(original)},
                    "patched": {"sha256": patched_digest, "size": len(patched)},
                    "backup": {
                        "path": f"backups/app.asar-{original_digest}.original",
                        "sha256": original_digest,
                        "size": len(original),
                    },
                }
            ),
            encoding="utf-8",
        )
        checks = self._checks(profile)
        self.assertEqual(checks["archive"].level, "PASS")
        self.assertEqual(checks["assets"].level, "PASS")
        self.assertIn("verified patched state", checks["archive"].detail)

        before = archive.read_bytes()
        backup.write_bytes(b"tampered")
        checks = self._checks(profile)
        self.assertEqual(checks["archive"].level, "FAIL")
        self.assertEqual(archive.read_bytes(), before)

    def test_browser_only_missing_physical_assets_is_failure(self) -> None:
        profile = self._profile(create_assets=False)
        checks = self._checks(profile)
        self.assertEqual(checks["assets"].level, "FAIL")
        self.assertIn("Missing directory", checks["assets"].detail)

        original = b"known original"
        profile = self._profile(
            mode="steam-electron",
            build_id="2002",
            archive="resources/app.asar",
            entry="electron/main.cjs",
            adapter="fractured_realms",
            archive_sha256=hashlib.sha256(original).hexdigest(),
            archive_size=len(original),
        )
        (self.steam_dir / "steamapps/common/Example Game/resources/app.asar").write_bytes(original)
        with mock.patch("crossover_electron_bridge.doctor._resource_is_file", return_value=False):
            checks = self._checks(profile)
        self.assertEqual(checks["runtime"].level, "FAIL")
        self.assertIn("resource", checks["runtime"].detail)

    def test_port_free_and_matching_health_identity(self) -> None:
        profile = self._profile(port=1)
        # Use a real ephemeral listener for the identity probe, then a released
        # port for the free result.  This exercises the same wire shape as the
        # shared BrowserServer rather than mocking HTTP parsing.
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
        port = int(server.server_address[1])
        _HealthHandler.payload = {
            "ok": True,
            "service": "example",
            "host": "127.0.0.1",
            "port": port,
        }
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        identity_profile = replace(profile, renderer=replace(profile.renderer, port=port))
        identity = self._checks(identity_profile)
        self.assertEqual(identity["port"].level, "PASS")
        self.assertIn("serving example", identity["port"].detail)

        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
        free = self._checks(profile)
        self.assertEqual(free["port"].level, "PASS")
        self.assertIn("available", free["port"].detail)

    def test_port_other_listener_is_failure(self) -> None:
        profile = self._profile(port=1)
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _HealthHandler)
        port = int(server.server_address[1])
        _HealthHandler.payload = {"ok": False}
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        self.addCleanup(server.server_close)
        checks = self._checks(replace(profile, renderer=replace(profile.renderer, port=port)))
        self.assertEqual(checks["port"].level, "FAIL")
        self.assertIn("another process", checks["port"].detail)


if __name__ == "__main__":
    unittest.main()
