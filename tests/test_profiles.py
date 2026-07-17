from __future__ import annotations

import dataclasses
import json
import shutil
import tempfile
import unittest
from pathlib import Path

from crossover_electron_bridge.errors import ConfigurationError
from crossover_electron_bridge.paths import (
    bottle_root,
    crossover_wine,
    steam_executable,
    steam_install,
    steam_manifest,
)
from crossover_electron_bridge.profiles import (
    GameProfile,
    RendererConfig,
    ResolvedProfile,
    SteamBuild,
    SteamConfig,
    load_profile,
    resolve_profile,
)


class ProfileContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile_dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.profile_dir)

    @staticmethod
    def _browser_document(profile_id: str = "external-browser") -> dict[str, object]:
        return {
            "schema_version": 1,
            "id": profile_id,
            "display_name": "External Browser",
            "mode": "browser-only",
            "bottle": "Steam",
            "steam": {
                "app_id": 123456,
                "install_dir": "Example Game",
                "supported_builds": [{"build_id": "1001"}],
            },
            "renderer": {
                "assets": "resources/app/dist",
                "bind_host": "127.0.0.1",
                "browser_host": "localhost",
                "port": 12345,
            },
            "adapter": "static",
            "state_key": "external-browser",
        }

    @staticmethod
    def _steam_document(profile_id: str = "external-steam") -> dict[str, object]:
        return {
            "schema_version": 1,
            "id": profile_id,
            "display_name": "External Steam Electron",
            "mode": "steam-electron",
            "bottle": "Steam",
            "steam": {
                "app_id": 987654,
                "install_dir": "Electron Game",
                "supported_builds": [
                    {
                        "build_id": "2002",
                        "archive_sha256": "a" * 64,
                        "archive_size": 1234,
                    }
                ],
            },
            "renderer": {
                "assets": "resources/app/dist",
                "bind_host": "127.0.0.1",
                "browser_host": "127.0.0.1",
                "port": 23456,
                "archive": "resources/app.asar",
                "entry": "electron/main.cjs",
            },
            "adapter": "fractured_realms",
            "state_key": "external-steam",
            "adapter_config": {"feature": "enabled"},
        }

    def _write(self, document: dict[str, object], profile_id: str | None = None) -> Path:
        profile_id = profile_id or str(document["id"])
        path = self.profile_dir / f"{profile_id}.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def _load_document(
        self,
        document: dict[str, object],
        profile_id: str | None = None,
    ) -> GameProfile:
        # Keep lookup independent from the document for malformed-profile tests;
        # in particular, a missing id must reach production validation.
        lookup_id = profile_id or str(document.get("id", "external-browser"))
        self._write(document, lookup_id)
        return load_profile(lookup_id, self.profile_dir)

    def test_external_browser_only_profile_and_exact_file_resolution(self) -> None:
        document = self._browser_document()
        self._write(document)
        # A profile directory is not scanned: the requested id maps exactly to id.json.
        self._write(self._browser_document("other"), "other")
        profile = load_profile("external-browser", self.profile_dir)

        self.assertIsInstance(profile, GameProfile)
        self.assertEqual(profile.id, "external-browser")
        self.assertEqual(profile.mode, "browser-only")
        self.assertEqual(profile.bottle, "Steam")
        self.assertEqual(profile.steam.app_id, 123456)
        self.assertEqual(profile.steam.install_dir, "Example Game")
        self.assertEqual(profile.steam.supported_builds, (SteamBuild("1001"),))
        self.assertEqual(profile.renderer, RendererConfig(
            assets="resources/app/dist",
            bind_host="127.0.0.1",
            browser_host="localhost",
            port=12345,
        ))
        self.assertEqual(profile.adapter, "static")
        self.assertEqual(dict(profile.adapter_config), {})

    def test_external_steam_electron_profile_and_fingerprint(self) -> None:
        profile = self._load_document(self._steam_document())

        self.assertIsInstance(profile, GameProfile)
        self.assertEqual(profile.mode, "steam-electron")
        self.assertEqual(profile.steam, SteamConfig(
            app_id=987654,
            install_dir="Electron Game",
            supported_builds=(SteamBuild("2002", "a" * 64, 1234),),
        ))
        self.assertEqual(profile.renderer.archive, "resources/app.asar")
        self.assertEqual(profile.renderer.entry, "electron/main.cjs")
        self.assertEqual(profile.adapter_config, {"feature": "enabled"})

    def test_models_are_frozen(self) -> None:
        profile = self._load_document(self._browser_document())
        self.assertTrue(dataclasses.is_dataclass(profile))
        with self.assertRaises(dataclasses.FrozenInstanceError):
            profile.id = "changed"  # type: ignore[misc]
        with self.assertRaises(dataclasses.FrozenInstanceError):
            profile.steam.app_id = 99  # type: ignore[misc]

    def test_unknown_top_level_key_is_rejected(self) -> None:
        document = self._browser_document()
        document["unexpected"] = True
        with self.assertRaises(ConfigurationError):
            self._load_document(document)

    def test_duplicate_json_keys_are_configuration_errors(self) -> None:
        raw = json.dumps(self._browser_document())
        duplicate = raw.replace(
            '"id": "external-browser"',
            '"id": "external-browser", "id": "external-browser"',
            1,
        )
        (self.profile_dir / "external-browser.json").write_text(
            duplicate,
            encoding="utf-8",
        )

        with self.assertRaises(ConfigurationError):
            load_profile("external-browser", self.profile_dir)

    def test_nonfinite_json_constants_are_configuration_errors(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                document = self._browser_document()
                document["adapter_config"] = {"nonfinite": value}
                self._write(document)

                with self.assertRaises(ConfigurationError):
                    load_profile("external-browser", self.profile_dir)

    def test_missing_required_top_level_keys_are_rejected(self) -> None:
        for key in self._browser_document():
            with self.subTest(key=key):
                document = self._browser_document()
                del document[key]
                with self.assertRaises(ConfigurationError):
                    self._load_document(document)

    def test_missing_required_nested_keys_are_rejected(self) -> None:
        sections = (
            ("steam", "app_id"),
            ("steam", "install_dir"),
            ("steam", "supported_builds"),
            ("renderer", "assets"),
            ("renderer", "bind_host"),
            ("renderer", "browser_host"),
            ("renderer", "port"),
        )
        for section, key in sections:
            with self.subTest(section=section, key=key):
                document = self._browser_document()
                section_data = dict(document[section])  # type: ignore[arg-type]
                del section_data[key]
                document[section] = section_data
                with self.assertRaises(ConfigurationError):
                    self._load_document(document)

        for key in ("build_id",):
            with self.subTest(section="supported_builds", key=key):
                document = self._browser_document()
                document["steam"] = {
                    **document["steam"],  # type: ignore[dict-item]
                    "supported_builds": [{}],
                }
                with self.assertRaises(ConfigurationError):
                    self._load_document(document)

    def test_missing_required_steam_fingerprint_keys_are_rejected(self) -> None:
        for missing in ("archive_sha256", "archive_size"):
            with self.subTest(missing=missing):
                document = self._steam_document()
                build = dict(document["steam"]["supported_builds"][0])  # type: ignore[index]
                del build[missing]
                document["steam"] = {
                    **document["steam"],  # type: ignore[dict-item]
                    "supported_builds": [build],
                }
                with self.assertRaises(ConfigurationError):
                    self._load_document(document)

    def test_absolute_and_traversal_install_relative_paths_are_rejected(self) -> None:
        for field in ("install_dir",):
            for value in ("/tmp/game", "../game", "nested/../../game", "nested/../game"):
                with self.subTest(field=field, value=value):
                    document = self._browser_document()
                    steam = dict(document["steam"])  # type: ignore[arg-type]
                    steam[field] = value
                    document["steam"] = steam
                    with self.assertRaises(ConfigurationError):
                        self._load_document(document)

        for field in ("assets", "archive", "entry"):
            for value in ("/tmp/game", "../game", "nested/../../game", "nested/../game"):
                with self.subTest(field=field, value=value):
                    document = self._steam_document()
                    renderer = dict(document["renderer"])  # type: ignore[arg-type]
                    renderer[field] = value
                    document["renderer"] = renderer
                    with self.assertRaises(ConfigurationError):
                        self._load_document(document)

    def test_non_normalized_relative_paths_are_rejected(self) -> None:
        cases = (
            ("bottle", self._browser_document, "nested//Steam"),
            ("install_dir", self._browser_document, "nested/./game"),
            ("assets", self._browser_document, "resources/app/dist/"),
            ("archive", self._steam_document, "resources//app.asar"),
            ("entry", self._steam_document, "electron/main.cjs/"),
        )
        for field, document_factory, value in cases:
            with self.subTest(field=field, value=value):
                document = document_factory()
                if field in {"bottle", "install_dir"}:
                    steam = dict(document["steam"])  # type: ignore[arg-type]
                    if field == "bottle":
                        document[field] = value
                    else:
                        steam[field] = value
                        document["steam"] = steam
                else:
                    renderer = dict(document["renderer"])  # type: ignore[arg-type]
                    renderer[field] = value
                    document["renderer"] = renderer
                with self.assertRaises(ConfigurationError):
                    self._load_document(document)

    def test_non_loopback_hosts_are_rejected(self) -> None:
        for section in ("bind_host", "browser_host"):
            document = self._browser_document()
            renderer = dict(document["renderer"])  # type: ignore[arg-type]
            renderer[section] = "192.0.2.10"
            document["renderer"] = renderer
            with self.subTest(section=section):
                with self.assertRaises(ConfigurationError):
                    self._load_document(document)

    def test_ipv6_hosts_follow_conservative_loopback_policy(self) -> None:
        for section, host in (("bind_host", "[::1]"), ("browser_host", "::1")):
            document = self._browser_document()
            renderer = dict(document["renderer"])  # type: ignore[arg-type]
            renderer[section] = host
            document["renderer"] = renderer
            with self.subTest(section=section, host=host):
                with self.assertRaises(ConfigurationError):
                    self._load_document(document)

    def test_invalid_ports_are_rejected(self) -> None:
        for port in (0, -1, 65536, "12345", True):
            with self.subTest(port=port):
                document = self._browser_document()
                renderer = dict(document["renderer"])  # type: ignore[arg-type]
                renderer["port"] = port
                document["renderer"] = renderer
                with self.assertRaises(ConfigurationError):
                    self._load_document(document)

    def test_schema_version_rejects_float_and_bool_values(self) -> None:
        for schema_version in (1.0, True):
            with self.subTest(schema_version=schema_version):
                document = self._browser_document()
                document["schema_version"] = schema_version
                with self.assertRaises(ConfigurationError):
                    self._load_document(document)

    def test_empty_and_duplicate_builds_are_rejected(self) -> None:
        for builds in ([], [{"build_id": ""}], [{"build_id": "1001"}, {"build_id": "1001"}]):
            with self.subTest(builds=builds):
                document = self._browser_document()
                document["steam"] = {
                    **document["steam"],  # type: ignore[dict-item]
                    "supported_builds": builds,
                }
                with self.assertRaises(ConfigurationError):
                    self._load_document(document)

    def test_steam_fingerprint_shape_is_validated(self) -> None:
        invalid_builds = (
            {"build_id": "2002", "archive_sha256": "not-a-hash", "archive_size": 1234},
            {"build_id": "2002", "archive_sha256": "a" * 63, "archive_size": 1234},
            {"build_id": "2002", "archive_sha256": "g" * 64, "archive_size": 1234},
            {"build_id": "2002", "archive_sha256": "a" * 64, "archive_size": 0},
            {"build_id": "2002", "archive_sha256": "a" * 64, "archive_size": -1},
            {"build_id": "2002", "archive_sha256": "a" * 64, "archive_size": "1234"},
        )
        for build in invalid_builds:
            with self.subTest(build=build):
                document = self._steam_document()
                document["steam"] = {
                    **document["steam"],  # type: ignore[dict-item]
                    "supported_builds": [build],
                }
                with self.assertRaises(ConfigurationError):
                    self._load_document(document)

    def test_mode_specific_fields_are_rejected(self) -> None:
        # Browser-only profiles cannot carry ASAR patch fields or fingerprints.
        browser = self._browser_document()
        browser_renderer = dict(browser["renderer"])  # type: ignore[arg-type]
        browser_renderer.update({"archive": "resources/app.asar", "entry": "electron/main.cjs"})
        browser["renderer"] = browser_renderer
        with self.assertRaises(ConfigurationError):
            self._load_document(browser)

        browser = self._browser_document()
        browser["steam"] = {
            **browser["steam"],  # type: ignore[dict-item]
            "supported_builds": [{
                "build_id": "1001",
                "archive_sha256": "a" * 64,
                "archive_size": 1,
            }],
        }
        with self.assertRaises(ConfigurationError):
            self._load_document(browser)

        # The static adapter has no ASAR patch implementation.
        steam = self._steam_document()
        steam["adapter"] = "static"
        with self.assertRaises(ConfigurationError):
            self._load_document(steam)

        # Steam-electron profiles must provide both renderer patch paths.
        for missing in ("archive", "entry"):
            steam = self._steam_document()
            renderer = dict(steam["renderer"])  # type: ignore[arg-type]
            del renderer[missing]
            steam["renderer"] = renderer
            with self.subTest(missing=missing):
                with self.assertRaises(ConfigurationError):
                    self._load_document(steam)

    def test_built_in_profiles_have_exact_contract_values(self) -> None:
        fractured = load_profile("fractured-realms")
        self.assertEqual(fractured.schema_version, 1)
        self.assertEqual(fractured.id, "fractured-realms")
        self.assertEqual(fractured.display_name, "Fractured Realms")
        self.assertEqual(fractured.mode, "steam-electron")
        self.assertEqual(fractured.bottle, "Steam")
        self.assertEqual(fractured.steam.app_id, 3789070)
        self.assertEqual(fractured.steam.install_dir, "Fractured Realms")
        self.assertEqual(fractured.steam.supported_builds, (
            SteamBuild("24185239", "3ba4d9b1b871a177f0a257dedc6eb229c4e195d0c05b2b5e00057f4581620c70", 186084852),
        ))
        self.assertEqual(fractured.renderer, RendererConfig(
            assets="resources/app/dist",
            bind_host="127.0.0.1",
            browser_host="127.0.0.1",
            port=48766,
            archive="resources/app.asar",
            entry="electron/main.cjs",
        ))
        self.assertEqual(fractured.adapter, "fractured_realms")
        self.assertEqual(fractured.state_key, "fractured-realms-main")
        self.assertEqual(dict(fractured.adapter_config), {})

        visseron = load_profile("visseron-idle")
        self.assertEqual(visseron.schema_version, 1)
        self.assertEqual(visseron.id, "visseron-idle")
        self.assertEqual(visseron.display_name, "Visseron Idle")
        self.assertEqual(visseron.mode, "browser-only")
        self.assertEqual(visseron.bottle, "Steam")
        self.assertEqual(visseron.steam.app_id, 4601670)
        self.assertEqual(visseron.steam.install_dir, "Visseron Idle")
        self.assertEqual(visseron.steam.supported_builds, (SteamBuild("24193581"),))
        self.assertEqual(visseron.renderer, RendererConfig(
            assets="resources/app/dist",
            bind_host="127.0.0.1",
            browser_host="localhost",
            port=8765,
        ))
        self.assertEqual(visseron.adapter, "visseron_idle")
        self.assertEqual(visseron.state_key, "visseron-idle")
        self.assertEqual(visseron.adapter_config["save_dir"], "~/.visseron-idle")
        self.assertEqual(
            visseron.adapter_config["wine_state_path"],
            "users/crossover/AppData/Roaming/visseron-idle",
        )
        self.assertEqual(visseron.adapter_config["max_request_bytes"], 4194304)
        self.assertEqual(visseron.adapter_config["max_backups"], 10)
        self.assertEqual(visseron.adapter_config["backup_interval_seconds"], 300)
        self.assertEqual(tuple(visseron.adapter_config["backup_slots"]), (1, 2, 3))

    def test_path_resolution_uses_synthetic_home(self) -> None:
        profile = self._load_document(self._steam_document())
        home = self.profile_dir / "home"
        resolved = resolve_profile(profile, home=home)

        self.assertIsInstance(resolved, ResolvedProfile)
        expected_bottle_root = home / "Library/Application Support/CrossOver/Bottles/Steam"
        self.assertEqual(resolved.profile, profile)
        self.assertEqual(resolved.bottle_root, expected_bottle_root)
        self.assertEqual(resolved.bottle_root, bottle_root("Steam", home))
        self.assertEqual(
            resolved.manifest,
            steam_manifest("Steam", 987654, home),
        )
        self.assertEqual(
            resolved.install,
            steam_install("Steam", 987654, "Electron Game", home),
        )
        self.assertEqual(
            resolved.steam_exe,
            steam_executable("Steam", home),
        )
        self.assertEqual(resolved.wine, crossover_wine())
        self.assertEqual(
            resolved.manifest,
            expected_bottle_root / "drive_c/Program Files (x86)/Steam/steamapps/appmanifest_987654.acf",
        )
        self.assertEqual(
            resolved.install,
            expected_bottle_root / "drive_c/Program Files (x86)/Steam/steamapps/common/Electron Game",
        )
        self.assertEqual(
            resolved.steam_exe,
            expected_bottle_root / "drive_c/Program Files (x86)/Steam/Steam.exe",
        )
        self.assertEqual(
            resolved.wine,
            Path("/Applications/CrossOver.app/Contents/SharedSupport/CrossOver/bin/wine"),
        )


if __name__ == "__main__":
    unittest.main()
