from __future__ import annotations

import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from crossover_electron_bridge import cli
from crossover_electron_bridge.doctor import CheckResult
from crossover_electron_bridge.profiles import load_profile


class CliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile_dir = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.profile_dir))

    @staticmethod
    def _external(profile_id: str = "external") -> dict[str, object]:
        return {
            "schema_version": 1,
            "id": profile_id,
            "display_name": "External",
            "mode": "browser-only",
            "bottle": "Steam",
            "steam": {
                "app_id": 1234,
                "install_dir": "External Game",
                "supported_builds": [{"build_id": "1"}],
            },
            "renderer": {
                "assets": "resources/app/dist",
                "bind_host": "127.0.0.1",
                "browser_host": "localhost",
                "port": 12345,
            },
            "adapter": "static",
            "state_key": "external",
        }

    def _write(self, payload: dict[str, object], name: str | None = None) -> None:
        profile_id = name or str(payload["id"])
        (self.profile_dir / f"{profile_id}.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )

    def test_profiles_lists_only_builtins_and_exact_columns(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = cli.main(["profiles"])
        self.assertEqual(status, 0)
        self.assertEqual(
            out.getvalue().splitlines(),
            [
                "fractured-realms\tsteam-electron\tFractured Realms",
                "visseron-idle\tbrowser-only\tVisseron Idle",
            ],
        )
        self.assertNotIn("demo", out.getvalue().lower())

    def test_global_profile_dir_loads_exact_external_profile(self) -> None:
        self._write(self._external())
        checks = [CheckResult("PASS", "synthetic", "ok")]
        out = io.StringIO()
        with mock.patch.object(cli, "run_doctor", return_value=checks) as doctor:
            with contextlib.redirect_stdout(out):
                status = cli.main(
                    ["--profile-dir", str(self.profile_dir), "doctor", "external"]
                )
        self.assertEqual(status, 0)
        doctor.assert_called_once()
        self.assertEqual(out.getvalue(), "PASS synthetic: ok\n")

    def test_unknown_profile_and_malformed_command_are_exit_two(self) -> None:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            status = cli.main(["doctor", "missing"])
        self.assertEqual(status, 2)
        self.assertIn("profile missing", err.getvalue())

        err.seek(0)
        err.truncate(0)
        with contextlib.redirect_stderr(err):
            status = cli.main(["not-a-command"])
        self.assertEqual(status, 2)
        self.assertTrue(err.getvalue())

    def test_doctor_warning_is_non_blocking(self) -> None:
        checks = [CheckResult("WARN", "build", "unlisted")]
        out = io.StringIO()
        with mock.patch.object(cli, "run_doctor", return_value=checks):
            with contextlib.redirect_stdout(out):
                status = cli.main(["doctor", "fractured-realms"])
        self.assertEqual(status, 0)
        self.assertEqual(out.getvalue(), "WARN build: unlisted\n")

    def test_operational_failure_is_exit_one_on_stderr(self) -> None:
        self._write(self._external())
        err = io.StringIO()
        with mock.patch.object(cli, "run_doctor", side_effect=RuntimeError("probe failed")):
            with contextlib.redirect_stderr(err):
                status = cli.main(
                    ["--profile-dir", str(self.profile_dir), "launch", "external"]
                )
        self.assertEqual(status, 1)
        self.assertIn("probe failed", err.getvalue())

        checks = [CheckResult("FAIL", "assets", "missing")]
        with mock.patch.object(cli, "run_doctor", return_value=checks), mock.patch.object(
            cli, "BrowserServer"
        ) as server:
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                status = cli.main(["--profile-dir", str(self.profile_dir), "launch", "external"])
        self.assertEqual(status, 1)
        self.assertEqual(out.getvalue(), "FAIL assets: missing\n")
        server.assert_not_called()

    def test_browser_launch_honors_no_open(self) -> None:
        self._write(self._external())
        fake_server = mock.Mock()
        with mock.patch.object(cli, "run_doctor", return_value=[]), mock.patch.object(
            cli, "get_browser_adapter", return_value=object()
        ) as get_adapter, mock.patch.object(cli, "BrowserServer", return_value=fake_server) as server:
            status = cli.main(
                ["--profile-dir", str(self.profile_dir), "launch", "external", "--no-open"]
            )
        self.assertEqual(status, 0)
        get_adapter.assert_called_once()
        server.assert_called_once()
        fake_server.serve_forever.assert_called_once_with(open_browser=False)

    def test_browser_mode_rejects_patch_and_restore(self) -> None:
        self._write(self._external())
        for command in ("patch", "restore"):
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                status = cli.main(
                    ["--profile-dir", str(self.profile_dir), command, "external"]
                )
            self.assertEqual(status, 2)
            self.assertIn(f"{command} is unavailable", err.getvalue())

    def test_steam_launch_patches_and_uses_main_game_argv(self) -> None:
        profile = load_profile("fractured-realms")
        resolved = mock.Mock(
            wine=Path("/Applications/CrossOver.app/Contents/SharedSupport/CrossOver/bin/wine"),
            steam_exe=Path("/bottle/Steam.exe"),
        )
        manager = mock.Mock()
        with mock.patch.object(cli, "run_doctor", return_value=[]), mock.patch.object(
            cli, "get_asar_adapter", return_value="asar"
        ) as get_adapter, mock.patch.object(cli, "PatchManager", return_value=manager), mock.patch.object(
            cli, "resolve_profile", return_value=resolved
        ), mock.patch.object(cli.subprocess, "Popen") as popen:
            status = cli.main(["launch", "fractured-realms", "--no-open"])
        self.assertEqual(status, 0)
        get_adapter.assert_called_once_with(profile)
        manager.patch.assert_called_once_with(profile, "asar")
        popen.assert_called_once_with(
            [
                str(resolved.wine),
                "--bottle",
                "Steam",
                "--no-wait",
                str(resolved.steam_exe),
                "-applaunch",
                "3789070",
                "--crossover-browser",
            ],
            shell=False,
        )

    def test_steam_patch_and_restore_call_manager(self) -> None:
        profile = load_profile("fractured-realms")
        manager = mock.Mock()
        with mock.patch.object(cli, "PatchManager", return_value=manager), mock.patch.object(
            cli, "get_asar_adapter", return_value="asar"
        ):
            self.assertEqual(cli.main(["patch", "fractured-realms"]), 0)
            self.assertEqual(cli.main(["restore", "fractured-realms"]), 0)
        manager.patch.assert_called_once_with(profile, "asar")
        manager.restore.assert_called_once_with(profile)


if __name__ == "__main__":
    unittest.main()
