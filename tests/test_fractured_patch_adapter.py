from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from crossover_electron_bridge.errors import OperationalError
from crossover_electron_bridge.patching import RuntimeResources
from crossover_electron_bridge.profiles import (
    GameProfile,
    RendererConfig,
    SteamBuild,
    SteamConfig,
)
from crossover_electron_bridge.adapters.fractured_realms import FracturedRealmsAdapter


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/fractured-realms/electron"
REVISION = "CROSSOVER_BROWSER_GAMES_FRACTURED_REALMS_V1"
REJECTION = "Unexpected Fractured Realms entry point for build 24185239"


class FracturedRealmsAdapterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.work = Path(self.temp.name)
        self.extracted = self.work / "app"
        shutil.copytree(FIXTURE.parent, self.extracted)
        self.profile = GameProfile(
            schema_version=1,
            id="fractured-realms",
            display_name="Fractured Realms",
            mode="steam-electron",
            bottle="Steam",
            steam=SteamConfig(
                app_id=3789070,
                install_dir="Fractured Realms",
                supported_builds=(
                    SteamBuild(
                        "24185239",
                        "3ba4d9b1b871a177f0a257dedc6eb229c4e195d0c05b2b5e00057f4581620c70",
                        186084852,
                    ),
                ),
            ),
            renderer=RendererConfig(
                assets="resources/app/dist",
                bind_host="127.0.0.1",
                browser_host="127.0.0.1",
                port=48766,
                archive="resources/app.asar",
                entry="electron/main.cjs",
            ),
            adapter="fractured_realms",
            state_key="fractured-realms-main",
        )
        self.host = self.work / "electron-host.cjs"
        self.node_adapter = self.work / "node-adapter.cjs"
        self.host.write_text("host fixture", encoding="utf-8")
        self.node_adapter.write_text("adapter fixture", encoding="utf-8")
        self.resources = RuntimeResources(self.host, self.node_adapter)
        self.adapter = FracturedRealmsAdapter()
        self.addCleanup(self.temp.cleanup)

    def _reset_case(self) -> None:
        self.temp.cleanup()
        self.setUp()

    def _entry(self) -> Path:
        return self.extracted / "electron/main.cjs"

    def _snapshot(self) -> dict[Path, bytes]:
        return {
            path.relative_to(self.extracted): path.read_bytes()
            for path in self.extracted.rglob("*")
            if path.is_file()
        }

    def _apply(self) -> None:
        self.adapter.apply(self.extracted, self.profile, self.resources)

    def test_installs_runtime_resources_and_exact_profile_contract(self) -> None:
        self._apply()
        electron = self.extracted / "electron"
        self.assertEqual((electron / "crossover-browser-host.cjs").read_text(), "host fixture")
        self.assertEqual((electron / "crossover-browser-adapter.cjs").read_text(), "adapter fixture")
        profile_path = electron / "crossover-browser-profile.json"
        document = json.loads(profile_path.read_text(encoding="utf-8"))
        self.assertEqual(
            document,
            {
                "schema_version": 1,
                "id": "fractured-realms",
                "display_name": "Fractured Realms",
                "service": REVISION,
                "assets_relative_to_runtime": "../dist",
                "bind_host": "127.0.0.1",
                "browser_host": "127.0.0.1",
                "port": 48766,
                "max_request_bytes": 65536,
            },
        )
        self.assertEqual(self.adapter.revision, REVISION)
        self.assertEqual(
            (electron / "main.cjs").read_text(encoding="utf-8").count("const fs   = require('fs');"),
            1,
        )
        transformed = self._entry().read_text(encoding="utf-8")
        self.assertEqual(transformed.count("function unlockSteamAchievement(apiName)"), 1)
        self.assertEqual(
            transformed.count(
                "ipcMain.handle('steam:unlock', (_event, apiName) => unlockSteamAchievement(apiName));"
            ),
            1,
        )
        self.assertEqual(transformed.count(REVISION), 1)
        self.assertEqual(document.keys(), {
            "schema_version", "id", "display_name", "service",
            "assets_relative_to_runtime", "bind_host", "browser_host",
            "port", "max_request_bytes",
        })

    def _run_entry(self, *, browser: bool, steam_mode: str = "client") -> dict[str, object]:
        """Run the transformed fixture with a tiny in-process Electron mock."""
        harness = r'''
const fs = require('fs');
const Module = require('module');
const vm = require('vm');
const entry = process.argv[2];
const browser = process.argv[3] === 'browser';
const steamMode = process.argv[4] || 'client';
const events = [];
const silentConsole = { log() {}, error() {}, warn() {} };
global.console = silentConsole;
const handlers = new Map();
const appHandlers = new Map();
const windows = [];
const app = {
  whenReady() { return { then(callback) { return Promise.resolve(callback()); } }; },
  on(name, callback) { appHandlers.set(name, callback); },
  quit() { events.push('quit'); },
};
function BrowserWindow() {
  events.push('createWindow');
  windows.push(this);
  this.webContents = {};
  this.loadFile = () => {};
  this.on = () => {};
}
BrowserWindow.getAllWindows = () => windows;
BrowserWindow.fromWebContents = () => null;
const ipcMain = { handle(name, callback) { handlers.set(name, callback); } };
const shell = { openExternal: async () => ({}) };
const steamClient = {
  achievement: {
    activate(name) {
      if (steamMode === 'throw') throw new Error('activation failed');
      return 7;
    },
  },
};
const steamworks = {
  init() {
    events.push('initSteam');
    return steamMode === 'none' ? null : steamClient;
  },
};
let hostConfig = null;
const host = {
  start(config) {
    hostConfig = config;
    events.push('hostStart');
  },
};
const originalLoad = Module._load;
Module._load = function(request, parent, isMain) {
  if (request === 'electron') return { app, BrowserWindow, ipcMain, shell };
  if (request === 'steamworks.js') return steamworks;
  if (request === './crossover-browser-host.cjs') return host;
  if (request === './crossover-browser-adapter.cjs') return {};
  if (request === 'path') return require('node:path');
  return originalLoad.call(this, request, parent, isMain);
};
process.argv = ['node', entry].concat(browser ? ['--crossover-browser'] : []);
require(entry);
setImmediate(() => {
  const result = {
    events,
    browserServiceDelegatesToNative: hostConfig ? JSON.stringify(hostConfig.services.steamUnlock('ACH_TEST')) === JSON.stringify(handlers.get('steam:unlock')(null, 'ACH_TEST')) : false,
    browserServiceResult: hostConfig ? hostConfig.services.steamUnlock('ACH_TEST') : null,
    browserServiceHasQuit: hostConfig ? typeof hostConfig.services.quitApp === 'function' : false,
    browserQuitWorks: hostConfig ? (() => { hostConfig.services.quitApp(); return events.includes('quit'); })() : false,
    nativeSuccess: handlers.has('steam:unlock') && steamMode === 'client' ? handlers.get('steam:unlock')(null, 'ACH_TEST') : null,
    nativeBadName: handlers.has('steam:unlock') && steamMode === 'client' ? handlers.get('steam:unlock')(null, null) : null,
    nativeNoClient: handlers.has('steam:unlock') && steamMode === 'none' ? handlers.get('steam:unlock')(null, 'ACH_TEST') : null,
    nativeCaughtError: handlers.has('steam:unlock') && steamMode === 'throw' ? handlers.get('steam:unlock')(null, 'ACH_TEST') : null,
    activationPreserved: false,
  };
  if (!browser && appHandlers.has('activate')) {
    windows.length = 0;
    appHandlers.get('activate')();
    result.activationPreserved = events.filter((event) => event === 'createWindow').length === 2;
  }
  process.stdout.write(JSON.stringify(result));
});
'''
        harness_path = self.work / "adapter-harness.cjs"
        harness_path.write_text(harness, encoding="utf-8")
        completed = subprocess.run(
            ["node", str(harness_path), str(self._entry()), "browser" if browser else "normal", steam_mode],
            capture_output=True,
            text=True,
        )
        if completed.returncode:
            self.fail(
                "Node harness failed with exit code "
                f"{completed.returncode}:\n"
                f"stdout:\n{completed.stdout}\n"
                f"stderr:\n{completed.stderr}"
            )
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            self.fail(
                "Node harness emitted invalid JSON: "
                f"{exc}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
            )

    def test_browser_mode_initializes_steam_before_host_and_keeps_services(self) -> None:
        self._apply()
        result = self._run_entry(browser=True)
        self.assertLess(result["events"].index("initSteam"), result["events"].index("hostStart"))
        self.assertTrue(result["browserServiceDelegatesToNative"])
        self.assertTrue(result["browserServiceHasQuit"])
        self.assertTrue(result["browserQuitWorks"])
        self.assertEqual(result["browserServiceResult"], {"ok": True, "activated": 7})
        self.assertNotIn("createWindow", result["events"])

    def test_normal_mode_keeps_window_activation_and_native_unlock_shapes(self) -> None:
        self._apply()
        result = self._run_entry(browser=False)
        self.assertEqual(result["events"].count("initSteam"), 1)
        self.assertNotIn("hostStart", result["events"])
        self.assertTrue(result["activationPreserved"])
        self.assertEqual(result["nativeSuccess"], {"ok": True, "activated": 7})
        self.assertEqual(result["nativeBadName"], {"ok": False, "reason": "bad-name"})

    def test_native_unlock_no_client_and_caught_error_are_preserved(self) -> None:
        self._apply()
        self.assertEqual(self._run_entry(browser=False, steam_mode="none")["nativeNoClient"], {
            "ok": False,
            "reason": "no-client",
        })
        self.assertEqual(self._run_entry(browser=False, steam_mode="throw")["nativeCaughtError"], {
            "ok": False,
            "error": "activation failed",
        })

    def test_each_missing_or_duplicate_main_anchor_fails_before_writes(self) -> None:
        anchors = (
            "const STEAM_APP_ID = 3789070;",
            "const fs   = require('fs');",
            "function initSteam() {",
            "function createWindow() {",
            "app.whenReady().then(() => {",
            "ipcMain.handle('open-external',",
            "ipcMain.handle('submit-feedback',",
            "ipcMain.handle('get-fullscreen',",
            "ipcMain.handle('set-fullscreen',",
            "ipcMain.handle('quit-app',",
            "ipcMain.handle('save-game',",
            "ipcMain.handle('steam:reset-achievements',",
            "ipcMain.handle('steam:unlock', (_event, apiName) => {",
        )
        for anchor in anchors:
            with self.subTest(anchor=anchor, kind="missing"):
                self._reset_case()
                source = self._entry().read_text(encoding="utf-8")
                self._entry().write_text(source.replace(anchor, "", 1), encoding="utf-8")
                before = self._snapshot()
                with self.assertRaisesRegex(OperationalError, f"^{REJECTION}$"):
                    self._apply()
                self.assertEqual(self._snapshot(), before)
                self.assertFalse((self.extracted / "electron/crossover-browser-profile.json").exists())
            with self.subTest(anchor=anchor, kind="duplicate"):
                self._reset_case()
                source = self._entry().read_text(encoding="utf-8")
                self._entry().write_text(source + "\n" + anchor + "\n", encoding="utf-8")
                before = self._snapshot()
                with self.assertRaisesRegex(OperationalError, f"^{REJECTION}$"):
                    self._apply()
                self.assertEqual(self._snapshot(), before)
                self.assertFalse((self.extracted / "electron/crossover-browser-profile.json").exists())

    def test_compact_demo_when_ready_fixture_is_rejected(self) -> None:
        source = self._entry().read_text(encoding="utf-8")
        demo = """const { app } = require('electron');
app.whenReady().then(() => {
  createWindow();
});
"""
        self._entry().write_text(demo, encoding="utf-8")
        before = self._snapshot()
        with self.assertRaisesRegex(OperationalError, f"^{REJECTION}$"):
            self._apply()
        self.assertEqual(self._snapshot(), before)


if __name__ == "__main__":
    unittest.main()
