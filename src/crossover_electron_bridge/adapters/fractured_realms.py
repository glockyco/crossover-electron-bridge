"""ASAR adapter for the purchased Fractured Realms main game.

The adapter is deliberately build-bound.  It recognizes only the observed
main-game entry point and performs all validation before touching the extracted
archive, so a demo entry point or an already-modified source cannot be
silently repacked as the purchased game.
"""

from __future__ import annotations

import json
import re
import stat
from pathlib import Path

from ..atomic import atomic_copy, atomic_write_text
from ..errors import OperationalError
from ..patching import RuntimeResources
from ..profiles import GameProfile


class FracturedRealmsAdapter:
    """Patch the purchased Fractured Realms Electron entry point."""

    revision = "CROSSOVER_BROWSER_GAMES_FRACTURED_REALMS_V1"

    _ERROR = "Unexpected Fractured Realms entry point for build 24185239"
    _BUILD_ID = "24185239"
    _APP_ID = 3789070
    _ARCHIVE = "resources/app.asar"
    _ENTRY = "electron/main.cjs"
    _ASSETS = "resources/app/dist"
    _PORT = 48766
    _MAX_REQUEST_BYTES = 65536
    _BIND_HOST = "127.0.0.1"
    _BROWSER_HOST = "127.0.0.1"

    _PRELOAD_NAMES = (
        "saveGame",
        "submitFeedback",
        "openExternal",
        "steamUnlock",
        "steamResetAchievements",
        "getFullscreen",
        "setFullscreen",
        "quitApp",
        "onFullscreenChanged",
    )

    @classmethod
    def _reject(cls) -> OperationalError:
        return OperationalError(cls._ERROR)

    @classmethod
    def _valid_profile(cls, profile: GameProfile) -> bool:
        """Check the complete identity/build contract before any filesystem write."""

        try:
            if (
                profile.schema_version != 1
                or profile.id != "fractured-realms"
                or profile.display_name != "Fractured Realms"
                or profile.mode != "steam-electron"
                or profile.adapter != "fractured_realms"
                or profile.state_key != "fractured-realms-main"
                or profile.bottle != "Steam"
            ):
                return False
            steam = profile.steam
            if steam.app_id != cls._APP_ID or steam.install_dir != "Fractured Realms":
                return False
            builds = steam.supported_builds
            if len(builds) != 1:
                return False
            build = builds[0]
            if (
                build.build_id != cls._BUILD_ID
                or build.archive_sha256
                != "3ba4d9b1b871a177f0a257dedc6eb229c4e195d0c05b2b5e00057f4581620c70"
                or build.archive_size != 186084852
            ):
                return False
            renderer = profile.renderer
            return (
                renderer.assets == cls._ASSETS
                and renderer.archive == cls._ARCHIVE
                and renderer.entry == cls._ENTRY
                and renderer.bind_host == cls._BIND_HOST
                and renderer.browser_host == cls._BROWSER_HOST
                and renderer.port == cls._PORT
            )
        except (AttributeError, TypeError, IndexError):
            return False

    @classmethod
    def _validate_entry(cls, extracted_root: Path, profile: GameProfile) -> tuple[Path, str, str]:
        """Read and validate all main/preload anchors without modifying the tree."""

        if not cls._valid_profile(profile):
            raise cls._reject()

        root = Path(extracted_root)
        entry = root / profile.renderer.entry
        preload = root / "electron" / "preload.cjs"
        if not root.is_dir() or not entry.is_file() or entry.is_symlink() or not preload.is_file() or preload.is_symlink():
            raise cls._reject()
        try:
            with entry.open("r", encoding="utf-8", newline="") as stream:
                source = stream.read()
            with preload.open("r", encoding="utf-8", newline="") as stream:
                preload_source = stream.read()
        except (OSError, UnicodeError):
            raise cls._reject()

        # These are the observed main-game anchors.  Counting exact source
        # fragments rejects both the demo and ambiguous/partially edited entry
        # points before any runtime file can be installed.
        main_anchors = (
            "const STEAM_APP_ID = 3789070;",
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
        if any(source.count(anchor) != 1 for anchor in main_anchors):
            raise cls._reject()

        if source.count("const path = require('path');") != 1:
            raise cls._reject()
        if source.count("const fs = require('fs');") != 0:
            raise cls._reject()
        if source.count("crossoverBrowserMode") != 0 or cls.revision in source:
            raise cls._reject()

        if preload_source.count("contextBridge.exposeInMainWorld('electronAPI', {") != 1:
            raise cls._reject()
        for name in cls._PRELOAD_NAMES:
            if len(re.findall(rf"(?m)^\s*{re.escape(name)}\s*:", preload_source)) != 1:
                raise cls._reject()

        # Requiring the complete native handler body ensures that the service
        # function below preserves each native return shape and error path.
        newline = "\r\n" if "\r\n" in source else "\n"
        native_handler = newline.join(
            (
                "ipcMain.handle('steam:unlock', (_event, apiName) => {",
                "  try {",
                "    if (!steamClient) { console.log('[steam] unlock skipped, no client:', apiName); return { ok: false, reason: 'no-client' }; }",
                "    if (typeof apiName !== 'string') return { ok: false, reason: 'bad-name' };",
                "    const res = steamClient.achievement.activate(apiName);",
                "    console.log('[steam] activate', apiName, '->', res);",
                "    return { ok: true, activated: res };",
                "  } catch (err) {",
                "    console.log('[steam] activate ERROR', apiName, err?.message ?? err);",
                "    return { ok: false, error: err?.message ?? String(err) };",
                "  }",
                "});",
            )
        )
        if source.count(native_handler) != 1:
            raise cls._reject()
        return entry, source, newline

    @classmethod
    def _patched_source(cls, source: str, newline: str) -> str:
        """Build the complete transformed source in memory."""

        native_handler = newline.join(
            (
                "ipcMain.handle('steam:unlock', (_event, apiName) => {",
                "  try {",
                "    if (!steamClient) { console.log('[steam] unlock skipped, no client:', apiName); return { ok: false, reason: 'no-client' }; }",
                "    if (typeof apiName !== 'string') return { ok: false, reason: 'bad-name' };",
                "    const res = steamClient.achievement.activate(apiName);",
                "    console.log('[steam] activate', apiName, '->', res);",
                "    return { ok: true, activated: res };",
                "  } catch (err) {",
                "    console.log('[steam] activate ERROR', apiName, err?.message ?? err);",
                "    return { ok: false, error: err?.message ?? String(err) };",
                "  }",
                "});",
            )
        )
        unlock_function = newline.join(
            (
                "function unlockSteamAchievement(apiName) {",
                "  try {",
                "    if (!steamClient) { console.log('[steam] unlock skipped, no client:', apiName); return { ok: false, reason: 'no-client' }; }",
                "    if (typeof apiName !== 'string') return { ok: false, reason: 'bad-name' };",
                "    const res = steamClient.achievement.activate(apiName);",
                "    console.log('[steam] activate', apiName, '->', res);",
                "    return { ok: true, activated: res };",
                "  } catch (err) {",
                "    console.log('[steam] activate ERROR', apiName, err?.message ?? err);",
                "    return { ok: false, error: err?.message ?? String(err) };",
                "  }",
                "}",
                "",
                "ipcMain.handle('steam:unlock', (_event, apiName) => unlockSteamAchievement(apiName));",
            )
        )
        source = source.replace(native_handler, unlock_function, 1)

        original_ready = newline.join(
            (
                "app.whenReady().then(() => {",
                "  initSteam();",
                "  createWindow();",
                "  app.on('activate', () => {",
                "    if (BrowserWindow.getAllWindows().length === 0) createWindow();",
                "  });",
                "});",
            )
        )
        browser_ready = newline.join(
            (
                "app.whenReady().then(async () => {",
                "  initSteam();",
                "  if (crossoverBrowserMode) {",
                "    await require('./crossover-browser-host.cjs').start({",
                "      app,",
                "      shell,",
                "      path,",
                "      fs,",
                "      profile: require('./crossover-browser-profile.json'),",
                "      adapter: require('./crossover-browser-adapter.cjs'),",
                "      services: {",
                "        steamUnlock: unlockSteamAchievement,",
                "        quitApp: () => app.quit(),",
                "      },",
                "    });",
                "    return;",
                "  }",
                "  createWindow();",
                "  app.on('activate', () => {",
                "    if (BrowserWindow.getAllWindows().length === 0) createWindow();",
                "  });",
                "});",
            )
        )
        if source.count(original_ready) != 1:
            raise cls._reject()
        source = source.replace(original_ready, browser_ready, 1)

        import_anchor = "const path = require('path');"
        import_replacement = newline.join(
            (
                import_anchor,
                "const fs = require('fs');",
                f"const crossoverBrowserMode = process.argv.includes('--crossover-browser'); // {cls.revision}",
            )
        )
        if source.count(import_anchor) != 1:
            raise cls._reject()
        source = source.replace(import_anchor, import_replacement, 1)
        return source

    @classmethod
    def _runtime_profile(cls, profile: GameProfile) -> str:
        payload = {
            "schema_version": 1,
            "id": profile.id,
            "display_name": profile.display_name,
            "service": cls.revision,
            "assets_relative_to_runtime": "../dist",
            "bind_host": cls._BIND_HOST,
            "browser_host": cls._BROWSER_HOST,
            "port": cls._PORT,
            "max_request_bytes": cls._MAX_REQUEST_BYTES,
        }
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"

    def apply(
        self,
        extracted_root: Path,
        profile: GameProfile,
        resources: RuntimeResources,
    ) -> None:
        """Validate and patch an extracted main-game ASAR tree."""

        entry, source, newline = self._validate_entry(Path(extracted_root), profile)
        # Materialize and validate every output before the first write.  This
        # includes the two packaged Node resources and the exact runtime JSON
        # contract, preventing validation errors from leaving partial files.
        try:
            host = Path(resources.electron_host)
            node_adapter = Path(resources.node_adapter)
        except (AttributeError, TypeError):
            raise self._reject()
        if not host.is_file() or host.is_symlink() or not node_adapter.is_file() or node_adapter.is_symlink():
            raise self._reject()
        runtime_profile = self._runtime_profile(profile)
        try:
            decoded_profile = json.loads(runtime_profile)
        except (TypeError, ValueError):
            raise self._reject()
        expected_keys = {
            "schema_version",
            "id",
            "display_name",
            "service",
            "assets_relative_to_runtime",
            "bind_host",
            "browser_host",
            "port",
            "max_request_bytes",
        }
        if set(decoded_profile) != expected_keys or decoded_profile["service"] != self.revision:
            raise self._reject()
        patched = self._patched_source(source, newline)

        electron_dir = entry.parent
        host_target = electron_dir / "crossover-browser-host.cjs"
        adapter_target = electron_dir / "crossover-browser-adapter.cjs"
        profile_target = electron_dir / "crossover-browser-profile.json"
        try:
            entry_mode = stat.S_IMODE(entry.stat().st_mode)
        except OSError:
            raise self._reject()

        # All writes are atomic and occur only after complete source/resource
        # validation above.  The extracted tree is disposable until packing.
        atomic_copy(host, host_target)
        atomic_copy(node_adapter, adapter_target)
        atomic_write_text(profile_target, runtime_profile, mode=0o600)
        atomic_write_text(entry, patched, mode=entry_mode or 0o600)


__all__ = ["FracturedRealmsAdapter"]
