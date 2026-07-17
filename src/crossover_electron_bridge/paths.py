"""CrossOver and Steam installation path helpers.

The helpers in this module are deliberately pure: they only construct paths and
never create directories or inspect the host filesystem.  Profile validation is
performed by :mod:`crossover_electron_bridge.profiles` before these helpers are
called.
"""

from __future__ import annotations

from pathlib import Path


CROSSOVER_WINE = Path(
    "/Applications/CrossOver.app/Contents/SharedSupport/CrossOver/bin/wine"
)
_CROSSOVER_ROOT = Path("Library/Application Support/CrossOver/Bottles")
_STEAM_RELATIVE = Path("drive_c/Program Files (x86)/Steam")


def bottle_root(bottle: str, home: Path = Path.home()) -> Path:
    """Return the CrossOver bottle root beneath *home*."""

    return Path(home) / _CROSSOVER_ROOT / bottle


def steam_directory(bottle: str, home: Path = Path.home()) -> Path:
    """Return the bottled Steam installation directory."""

    return bottle_root(bottle, home) / _STEAM_RELATIVE


def steam_manifest(bottle: str, app_id: int, home: Path = Path.home()) -> Path:
    """Return Steam's manifest path for an application."""

    return steam_directory(bottle, home) / "steamapps" / f"appmanifest_{app_id}.acf"


def steam_install(
    bottle: str, app_id: int, install_dir: str, home: Path = Path.home()
) -> Path:
    """Return a game's install directory in Steam's ``common`` directory.

    ``app_id`` is accepted so callers can derive all paths from one validated
    profile, even though Steam's common directory itself is app-id independent.
    """

    del app_id
    return steam_directory(bottle, home) / "steamapps" / "common" / install_dir


def steam_executable(bottle: str, home: Path = Path.home()) -> Path:
    """Return the bottled ``Steam.exe`` path."""

    return steam_directory(bottle, home) / "Steam.exe"


def crossover_wine() -> Path:
    """Return the fixed CrossOver Wine executable path."""

    return CROSSOVER_WINE


__all__ = [
    "CROSSOVER_WINE",
    "bottle_root",
    "steam_directory",
    "steam_manifest",
    "steam_install",
    "steam_executable",
    "crossover_wine",
]
