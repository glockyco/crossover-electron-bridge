# CrossOver Electron Bridge

CrossOver Electron Bridge runs supported Windows Steam Electron games installed through CrossOver in a native macOS browser. It supports Fractured Realms and Visseron Idle through game-specific adapters, not arbitrary Electron games.

This project is independent and is not affiliated with or endorsed by CodeWeavers.

## Setup

You need macOS, CrossOver, and a supported game installed through Steam in the bottle named by its [profile](src/crossover_electron_bridge/profiles/). Install Python as required by [pyproject.toml](pyproject.toml), and Node.js with `npx` for Fractured Realms patching. The Python package has no runtime dependencies.

From the repository root:

```sh
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --editable .
crossover-electron-bridge profiles
```

The profiles define the bottle, installed game, supported builds, browser address, and adapter settings. The bridge expects CrossOver at the location defined in [paths.py](src/crossover_electron_bridge/paths.py).

## Play a game

Close the game first, then check your installation:

```sh
crossover-electron-bridge doctor fractured-realms
crossover-electron-bridge doctor visseron-idle
```

`doctor` is read-only. It checks the installation, game assets, adapter, browser port, and patch recovery state where applicable. Resolve failures before launching. `launch` repeats these checks before starting the game.

```sh
crossover-electron-bridge launch fractured-realms
crossover-electron-bridge launch visseron-idle
```

You can also double-click [Fractured Realms.command](fractured-realms/Fractured%20Realms.command) or [Visseron Idle.command](visseron-idle/Visseron%20Idle.command) in Finder. These launchers use the source checkout without installing the package, but still need Python and the game requirements.

### Fractured Realms

Fractured Realms uses the `steam-electron` mode. Launch patches the installed Electron archive, starts the game through bottled Steam, and opens its interface in your browser. The Electron process retains Steam tracking, presence, playtime, and achievement integration.

Only the main game and the build and archive fingerprint listed in its [profile](src/crossover_electron_bridge/profiles/fractured-realms.json) are accepted. A Steam update can make the profile incompatible. Do not bypass a build or fingerprint failure.

### Visseron Idle

Visseron Idle uses the `browser-only` mode. The bridge serves the installed game's interface without patching its archive. Keep the command running while you play, and stop it with Ctrl+C when finished.

To start the server without opening a browser:

```sh
crossover-electron-bridge launch visseron-idle --no-open
```

An unlisted Steam build produces a warning in this mode. Missing assets, a missing adapter, or a conflicting port still prevent launch. This mode does not provide Steam tracking or achievements.

Neither mode adds Steam Cloud synchronization or an overlay in the browser. Browser storage belongs to the exact browser address and browser profile, so changing either can make existing browser data inaccessible.

## Files and network access

Installing the Python package does not patch game files. Fractured Realms launch and `patch` replace its installed Electron archive and keep an original backup with recovery metadata in the local state directory defined by [patching.py](src/crossover_electron_bridge/patching.py).

Visseron Idle imports a valid Wine save when no browser-mode save exists. It writes saves, manual saves, and rolling backups to the save directory in its [profile](src/crossover_electron_bridge/profiles/visseron-idle.json). Its backup controls also read backup slots from the configured Wine save directory.

The bridge serves game assets on loopback and requires a per-process token for its APIs. ASAR patching invokes `npx`, which can download the Electron ASAR tool. Fractured Realms forwards achievement requests to Steam. If you submit in-game feedback and a webhook is configured, its [adapter](src/crossover_electron_bridge/adapters/fractured-realms.cjs) sends the message, contact details, and supplied context to that webhook.

## Restore Fractured Realms

Close the game before patching or restoring. To patch without launching, or restore the original archive:

```sh
crossover-electron-bridge patch fractured-realms
crossover-electron-bridge restore fractured-realms
```

Restore replaces the archive only when the installed build, patched archive, recovery metadata, and original backup agree. Mismatched state stops the operation without replacing the archive. Keep the recovery files, which remain after a successful restore. A game update needs a reviewed profile and adapter update before patching an unsupported build.

## Development

[cli.py](src/crossover_electron_bridge/cli.py) defines the commands. [profiles.py](src/crossover_electron_bridge/profiles.py) validates profiles, [adapters](src/crossover_electron_bridge/adapters/) holds the game integrations, and [runtime](src/crossover_electron_bridge/runtime/) holds the Electron host.

From the repository root, with the Python environment active:

```sh
python3 -m unittest discover -s tests
node --test tests/node/*.test.cjs
```

[CI](.github/workflows/ci.yml) runs both suites on macOS for every pull request.

For commands without a package install, use the source module:

```sh
PYTHONPATH=src python3 -m crossover_electron_bridge profiles
```

The global `--profile-dir PATH` option loads named JSON profiles from an extension directory. Put it before the subcommand. Profiles can select registered adapters but cannot load executable code from that directory. Add or change an adapter for a game whose native services or interface need different handling.
