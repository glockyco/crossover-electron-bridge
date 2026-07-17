from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from crossover_electron_bridge.errors import ConfigurationError, OperationalError
from crossover_electron_bridge.steam import read_steam_manifest


class SteamManifestTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.directory)
        self.path = self.directory / "appmanifest_3789070.acf"

    def write(self, content: str) -> None:
        self.path.write_text(content, encoding="utf-8")

    def test_reads_real_shaped_app_state_and_preserves_values(self) -> None:
        self.write(
            '"AppState"\n'
            "{\n"
            '    "appid"\t"3789070"\n'
            '    "Universe" "1"\n'
            '    "name" "Fractured Realms:  Main Game"\n'
            '    "StateFlags" "4"\n'
            '    "installdir" "Fractured Realms  "\n'
            '    "LastUpdated" "0"\n'
            '    "buildid" "24185239"\n'
            '    "InstalledDepots"\n'
            "    {\n"
            '        "3789071" { "manifest" "abc" }\n'
            "    }\n"
            "}\n"
        )

        self.assertEqual(
            read_steam_manifest(self.path),
            {
                "appid": "3789070",
                "name": "Fractured Realms:  Main Game",
                "installdir": "Fractured Realms  ",
                "buildid": "24185239",
            },
        )

    def test_missing_file_is_operational_error(self) -> None:
        with self.assertRaises(OperationalError):
            read_steam_manifest(self.path)

    def test_unreadable_encoding_is_operational_error(self) -> None:
        self.path.write_bytes(b'"AppState" { "appid" "\xff" }')
        with self.assertRaises(OperationalError):
            read_steam_manifest(self.path)

    def test_missing_required_fields_are_configuration_errors(self) -> None:
        for missing in ("appid", "name", "installdir", "buildid"):
            with self.subTest(missing=missing):
                fields = {
                    "appid": "3789070",
                    "name": "Fractured Realms",
                    "installdir": "Fractured Realms",
                    "buildid": "24185239",
                }
                del fields[missing]
                self.write(
                    '"AppState" {\n'
                    + "\n".join(f'"{key}" "{value}"' for key, value in fields.items())
                    + "\n}"
                )
                with self.assertRaises(ConfigurationError):
                    read_steam_manifest(self.path)

    def test_duplicate_required_fields_are_rejected(self) -> None:
        for duplicate in ("appid", "name", "installdir", "buildid"):
            with self.subTest(duplicate=duplicate):
                self.write(
                    '"AppState" { '
                    '"appid" "3789070" '
                    '"name" "Fractured Realms" '
                    '"installdir" "Fractured Realms" '
                    '"buildid" "24185239" '
                    f'"{duplicate}" "second" }}'
                )
                with self.assertRaises(ConfigurationError):
                    read_steam_manifest(self.path)

    def test_empty_required_values_are_rejected(self) -> None:
        for empty in ("appid", "name", "installdir", "buildid"):
            with self.subTest(empty=empty):
                values = {
                    "appid": "3789070",
                    "name": "Fractured Realms",
                    "installdir": "Fractured Realms",
                    "buildid": "24185239",
                }
                values[empty] = ""
                self.write(
                    '"AppState" { '
                    + " ".join(f'"{key}" "{value}"' for key, value in values.items())
                    + " }"
                )
                with self.assertRaises(ConfigurationError):
                    read_steam_manifest(self.path)

    def test_malformed_braces_quotes_and_lines_are_rejected(self) -> None:
        malformed = (
            '"AppState" { "appid" "3789070" "name" "Fractured Realms" '
            '"installdir" "Fractured Realms" "buildid" "24185239"',
            '"AppState" { "appid" "3789070" "name" "Fractured Realms" '
            '"installdir" "Fractured Realms" "buildid" "24185239" }} extra',
            '"AppState" { "appid" 3789070 "name" "Fractured Realms" }',
            '"AppState" [ "appid" "3789070" ]',
            '"AppState" { "appid" "3789070" "name" "Fractured Realms" '
            '"installdir" "Fractured Realms" "buildid" "24185239" } trailing',
            '"AppState" { "appid" "3789070 }',
            '"AppState" { "appid" "3789070" "name" "Fractured Realms" '
            '"installdir" "Fractured Realms" "buildid" "24185239" } }',
        )
        for content in malformed:
            with self.subTest(content=content):
                self.write(content)
                with self.assertRaises(ConfigurationError):
                    read_steam_manifest(self.path)

    def test_nested_required_lookalikes_do_not_satisfy_top_level_fields(self) -> None:
        self.write(
            '"AppState" { '
            '"appid" "3789070" '
            '"name" "Fractured Realms" '
            '"installdir" "Fractured Realms" '
            '"metadata" { "buildid" "nested-only" } '
            "}"
        )
        with self.assertRaises(ConfigurationError):
            read_steam_manifest(self.path)

    def test_nested_non_required_data_is_accepted(self) -> None:
        self.write(
            '"AppState" { '
            '"appid" "3789070" '
            '"name" "Fractured Realms" '
            '"installdir" "Fractured Realms" '
            '"buildid" "24185239" '
            '"InstalledDepots" { "3789071" { "manifest" "abc" } } '
            '"UserConfig" { "language" "english" } '
            "}"
        )
        self.assertEqual(read_steam_manifest(self.path)["buildid"], "24185239")

    def test_duplicate_irrelevant_definitions_are_rejected(self) -> None:
        self.write(
            '"AppState" { '
            '"appid" "3789070" '
            '"name" "Fractured Realms" '
            '"installdir" "Fractured Realms" '
            '"buildid" "24185239" '
            '"StateFlags" "4" "StateFlags" "4" '
            "}"
        )
        with self.assertRaises(ConfigurationError):
            read_steam_manifest(self.path)


if __name__ == "__main__":
    unittest.main()
