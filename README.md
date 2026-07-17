# CrossOver Browser Games

CrossOver Browser Games launches the Windows Steam builds of **Fractured Realms Demo** and **Visseron Idle** from CrossOver's `Steam` bottle while rendering their web UI in a normal macOS browser. This browser path is needed because the native Electron window can be blank or transparent under CrossOver (and Steam's overlay renderer can fail to create its CEF paint event). Fractured keeps its Steam-tracked Electron process alive while the browser provides the visible game window.

## Compatibility and extensibility

This is currently a **two-profile proof of concept**, not an arbitrary-game tool. Arbitrary games do **not** work out of the box. The tiers below describe what a new title can reasonably require:

- **Automatic:** ordinary browser-compatible static renderers can use the existing browser-host approach with little or no title-specific code.
- **Profile-only:** install paths, ASAR details, entry points, browser origin, save location, and launch metadata can be captured as profile configuration when the renderer already fits the host.
- **Adapter-required:** preload IPC; filesystem, dialog, or shell APIs; custom schemes; native modules; persistence migration; and Steamworks integration require a reviewed title adapter. These are manual integration points, not automatic compatibility.
- **Unsupported:** native/GPU/plugin UI, DRM- or anti-cheat-coupled rendering, and undocumented encrypted or native state are outside the current browser approach.

### New-title onboarding

Before adding a title, document and verify:

1. Legal permission to redistribute, patch, or launch it in this project.
2. A build fingerprint and the exact install/update assumptions.
3. A renderer, preload, and IPC inventory, including every browser/native boundary.
4. A profile or adapter decision, with its security allowlist.
5. Save migration and rollback behavior, including failure recovery.
6. Deterministic fixtures for assets, IPC, saves, and recovery cases.
7. Clean-install, update, offline, browser, and CrossOver verification.
8. Known limitations, including any unavailable Steam features or state paths.

### Roadmap

First secure the two existing profiles; then extract a common host, profile schema, adapter ABI, and diagnostic (`doctor`) command. Next add fixtures and signed releases, and publish only profiles that have been verified end to end. This roadmap does not promise Cloud saves, overlays, or achievements.

## Requirements

- macOS
- CrossOver with a bottle named `Steam`
- The Windows Steam installation of each game in that bottle
- Python 3
- Node.js, including `npx`, for Fractured Realms patching

## Quick start

Double-click either launcher:

- [Fractured Realms.command](fractured-realms/Fractured%20Realms.command)
- [Visseron Idle.command](visseron-idle/Visseron%20Idle.command)

From this project's root directory, the equivalent command-line launches are:

```sh
python3 fractured-realms/patch-and-launch.py
python3 visseron-idle/server.py
```

Both launchers open the browser automatically. To start Visseron's server without opening a browser, use:

```sh
python3 visseron-idle/server.py --no-open
```

The `.command` files locate their sibling scripts from the project directory, so they do not depend on copies in the home directory.

## Fractured Realms Demo

`patch-and-launch.py` patches the installed `resources/app.asar` to route the Electron web UI to the browser host, then starts Steam through CrossOver with the real game AppID (`4750500`). The patch is safe to repeat: a Steam update that replaces `app.asar` is detected and repatched on the next launch, with a fresh unpatched backup.

Useful recovery and maintenance commands:

```sh
# Patch the installed archive, but do not start Steam
python3 fractured-realms/patch-and-launch.py --patch-only

# Restore the latest unpatched archive saved by the patcher
python3 fractured-realms/patch-and-launch.py --restore
```

`--restore` restores the hash-bound original saved under `~/.local/state/crossover-browser-games/fractured-realms` only when the installed archive exactly matches the recorded patched build. If Steam has updated or replaced the archive, restore fails without changing files. It does not remove the game or any save data.

### Steam limitations

Fractured keeps the real AppID `4750500` Electron process alive. That preserves Steam's launch identity, presence, and playtime tracking while the browser renders the UI. The current local metadata exposes zero demo achievements and no Steam Cloud mapping, and the shipped code has no Steamworks API. Do not rely on achievements, the overlay, or Cloud saves in browser mode.

## Visseron Idle

`server.py` serves the installed game's web assets at `http://localhost:8765` and supplies the browser-side IPC shim used for saving and loading. It imports an existing Wine-bottle save on first run when needed, then keeps browser-mode saves under `~/.visseron-idle` (including its manual save and rolling backup files).

Visseron browser mode fully bypasses Steam: Steam is only where the installed files are found, and no Steam integration is provided by this mode. Stop the local server with `Ctrl-C`.

## Browser storage and recovery

Browser `localStorage` is origin-specific. Fractured's host listens at `http://127.0.0.1:48765`; Visseron's server uses `http://localhost:8765`. Data saved by a game at one exact scheme/host/port is not shared with another browser origin, hostname, port, or browser profile. Visseron's game saves are server-backed in `~/.visseron-idle`, so clearing browser data does not remove those files. Fractured's local host belongs to the launched Electron process and closes when that process exits.

## Project layout

```text
README.md
fractured-realms/
├── Fractured Realms.command
├── patch-and-launch.py
└── browser-host.cjs
visseron-idle/
├── Visseron Idle.command
└── server.py
```
