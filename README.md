# CrossOver Electron Bridge

Run compatible Electron games through CrossOver in a native macOS browser with profile-based patching and IPC bridges.

This project is independent and is not affiliated with or endorsed by CodeWeavers. It provides a small, profile-driven bridge for the supported games installed in a CrossOver bottle. It is not a general-purpose compatibility layer: games with native, GPU/plugin, DRM, anti-cheat, encrypted, or otherwise undocumented UI/state may require a reviewed adapter or may be unsupported.

## Requirements

- macOS
- CrossOver with a bottle named `Steam`
- The supported Windows games installed in that bottle through Steam
- Python 3.11 or newer
- Node.js with `npx` for ASAR patching and the `steam-electron` launch path

The Python package has no runtime dependencies. CrossOver and the installed game files remain outside this repository.

## Install

From the repository root, create an environment and install the package in editable mode:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install --editable .
source .venv/bin/activate
crossover-electron-bridge profiles
```

For a source checkout without installing the package, prefix the commands with `PYTHONPATH=src` and run `python3 -m crossover_electron_bridge` instead of the installed `crossover-electron-bridge` executable. The `.command` launchers do this source-path setup automatically.

Profiles can be loaded from a profile-only extension directory with the global option `--profile-dir PATH`. The command then resolves only `PATH/<profile-id>.json`; profiles cannot load executable code from that directory and must refer to an adapter registered by this package.

## Commands

The unified CLI exposes these commands:

```sh
crossover-electron-bridge profiles
crossover-electron-bridge doctor fractured-realms
crossover-electron-bridge doctor visseron-idle
crossover-electron-bridge launch fractured-realms
crossover-electron-bridge launch visseron-idle
crossover-electron-bridge launch visseron-idle --no-open
crossover-electron-bridge patch fractured-realms
crossover-electron-bridge restore fractured-realms
```

`doctor PROFILE` performs blocking, read-only checks for macOS, CrossOver, the bottle, Steam installation and manifest, renderer assets, registered adapters, the configured port, and (for `steam-electron`) the supported build and ASAR recovery state. `launch PROFILE` runs those checks first. Use `--no-open` for a browser-only profile when the server should start without opening a browser. `patch` and `restore` are available only for `steam-electron` profiles.

The same commands from a checkout are:

```sh
PYTHONPATH=src python3 -m crossover_electron_bridge profiles
PYTHONPATH=src python3 -m crossover_electron_bridge doctor fractured-realms
PYTHONPATH=src python3 -m crossover_electron_bridge launch fractured-realms
PYTHONPATH=src python3 -m crossover_electron_bridge launch visseron-idle --no-open
PYTHONPATH=src python3 -m crossover_electron_bridge patch fractured-realms
PYTHONPATH=src python3 -m crossover_electron_bridge restore fractured-realms
```

Put `--profile-dir PATH` before the subcommand, for example:

```sh
crossover-electron-bridge --profile-dir ./profiles doctor my-profile
```

## Finder launchers

Double-click either launcher from Finder, or run it as an executable:

- [Fractured Realms.command](fractured-realms/Fractured%20Realms.command) launches `fractured-realms` through the shared `steam-electron` path.
- [Visseron Idle.command](visseron-idle/Visseron%20Idle.command) launches `visseron-idle` through the shared `browser-only` path.

Both launchers derive the repository root and prepend its `src` directory to `PYTHONPATH`; they do not depend on copied scripts in the home directory. They open the browser by default.

## Profiles and compatibility

The built-in profiles deliberately describe two different modes:

- **`steam-electron`** patches a build-bound Electron ASAR, starts the real bottled Steam process, and lets that process retain native services while the browser renders the UI. An unknown Steam build or an archive whose recorded SHA-256/size does not match is a blocking error; patching fails closed before extraction or replacement.
- **`browser-only`** serves an installed renderer from the shared Python browser host and uses a registered adapter for its browser IPC. It does not patch an ASAR. An unlisted Steam build is a non-blocking warning, but missing assets, a missing adapter, or a conflicting port remains blocking.

Profiles validate install-relative paths, loopback binding, browser origins, ports, supported builds, and mode-specific fields. APIs exposed by adapters are authenticated per process; static assets and health checks remain available only through the configured local host.

### Fractured Realms (main game)

The `fractured-realms` profile targets the purchased main game only:

- Steam AppID: `3789070`
- CrossOver bottle: `Steam`
- Install directory: `Fractured Realms`
- Observed Steam build: `24185239`
- Package version: `0.2.2`
- ASAR: `resources/app.asar`
- Electron entry: `electron/main.cjs`
- Renderer assets: `resources/app/dist`
- Browser origin: `http://127.0.0.1:48766` (binds to `127.0.0.1`)
- Patch state: `~/.local/state/crossover-electron-bridge/fractured-realms-main`

The verified unpatched archive is 186,084,852 bytes with SHA-256 `3ba4d9b1b871a177f0a257dedc6eb229c4e195d0c05b2b5e00057f4581620c70`. The profile accepts that build and fingerprint only. The first successful main-game launch creates only the `fractured-realms-main` state directory and uses only the dedicated `127.0.0.1:48766` browser origin; it does not migrate browser storage or recovery files from another profile.

The patched Electron process is still launched by bottled Steam with AppID `3789070` and the `--crossover-browser` flag. That preserves Steam process tracking, presence, and playtime, and the browser bridge forwards achievement unlock requests to the native `steamworks.js` process. There is no Steam Cloud bridge in this project, and the Steam overlay cannot appear in the external browser. Do not treat browser local storage as Steam Cloud storage.

### Visseron Idle

The `visseron-idle` profile is `browser-only`:

- Steam AppID used to locate the install: `4601670`
- CrossOver bottle: `Steam`
- Install directory: `Visseron Idle`
- Observed build: `24193581` (build drift warns because this mode does not patch ASAR files)
- Browser origin: `http://localhost:8765` (binds to `127.0.0.1`)
- Browser save directory: `~/.visseron-idle`
- Existing Wine save source: `users/crossover/AppData/Roaming/visseron-idle` inside the bottle

On first run, the adapter imports an existing Wine save when needed. Browser-mode saves, manual saves, prior-generation preservation, rolling backups, and configured Wine backup slots remain under the existing `~/.visseron-idle` path. The browser host preserves the adapter's load/save, backup, restore, and delete routes. This mode does not provide Steam tracking, Steam Cloud, overlay, or achievement integration.

## Recovery and restore

For a `steam-electron` profile, patching records metadata version `2` and keeps an immutable original archive backup at:

```text
~/.local/state/crossover-electron-bridge/fractured-realms-main/backups/app.asar-<sha256>.original
```

Use the unified commands rather than copying an ASAR manually:

```sh
crossover-electron-bridge patch fractured-realms
crossover-electron-bridge restore fractured-realms
```

`patch` is idempotent only for a verified matching patched archive. Before any replacement it rechecks the Steam build and live archive fingerprint. `restore` changes the installed archive only when the current build, patch marker, recorded metadata, patched fingerprint, and immutable backup all agree; stale or mismatched state fails without changing the archive. Metadata and the immutable backup are retained after a successful restore for recovery evidence. A Steam update that changes the build or ASAR fingerprint must be reviewed and reflected in the profile and patch adapter before it can be patched.

## Storage and privacy boundaries

Browser storage is origin-specific. The Fractured Realms main game uses `http://127.0.0.1:48766`; Visseron uses `http://localhost:8765`. Data stored by one exact scheme/host/port is not shared with another origin, hostname, port, or browser profile. The bridge binds to loopback, serves files beneath the configured renderer assets, bounds request bodies, and requires a per-process token for API requests.

The project does not modify CrossOver archives or external recovery state merely by installing the Python package. Always close the relevant game before patching or restoring, and use `doctor` to diagnose the installed build and current port before launch.

## Project layout

```text
README.md
pyproject.toml
src/crossover_electron_bridge/
├── profiles/       # validated built-in profile JSON
├── adapters/       # registered Python and Electron adapters
├── runtime/        # packaged Electron host runtime
└── *.py            # profile, patch, browser, doctor, and CLI core
fractured-realms/
└── Fractured Realms.command
visseron-idle/
└── Visseron Idle.command
```
