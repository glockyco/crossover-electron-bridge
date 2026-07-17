"""Command-line interface for CrossOver Electron Bridge.

The CLI deliberately keeps policy at the command boundary: profiles are loaded
and validated before an operation starts, doctor checks are printed for every
launch, and configuration errors are distinguished from runtime failures.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from .adapters import get_asar_adapter, get_browser_adapter
from .browser import BrowserServer
from .doctor import CheckResult, run_doctor
from .errors import ConfigurationError
from .patching import PatchManager
from .profiles import GameProfile, load_profile, resolve_profile

# Built-ins are intentionally explicit.  A profile directory is an extension
# lookup for named commands, not an input to ``profiles`` and not a directory
# scan that could accidentally expose a retired title.
BUILTIN_PROFILE_IDS = ("fractured-realms", "visseron-idle")


class _ArgumentParser(argparse.ArgumentParser):
    """ArgumentParser whose errors are converted by :func:`main` to exit 2."""


def _parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(prog="crossover-electron-bridge")
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=None,
        metavar="PATH",
        help="load named profiles from PATH instead of packaged profiles",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("profiles", help="list packaged profiles")

    for command in ("doctor", "patch", "restore"):
        command_parser = commands.add_parser(command)
        command_parser.add_argument("profile")

    launch = commands.add_parser("launch")
    launch.add_argument("profile")
    launch.add_argument("--no-open", action="store_true", help="do not open a browser")
    return parser


def _print_doctor(checks: Sequence[CheckResult]) -> bool:
    """Print doctor checks and report whether they are blocking."""

    failed = False
    for check in checks:
        print(f"{check.level} {check.name}: {check.detail}")
        if check.level == "FAIL":
            failed = True
    return failed


def _run_doctor(profile: GameProfile) -> int:
    """Run and print checks, returning the doctor's process status."""

    return 1 if _print_doctor(run_doctor(profile)) else 0


def _load_named_profile(profile_id: str, profile_dir: Path | None) -> GameProfile:
    """Load a profile through the strict package loader."""

    return load_profile(profile_id, profile_dir)


def _profiles() -> int:
    for profile_id in BUILTIN_PROFILE_IDS:
        profile = _load_named_profile(profile_id, None)
        print(f"{profile.id}\t{profile.mode}\t{profile.display_name}")
    return 0


def _launch_browser(profile: GameProfile, *, no_open: bool) -> int:
    adapter = get_browser_adapter(profile)
    server = BrowserServer(profile, adapter)
    server.serve_forever(open_browser=not no_open)
    return 0


def _launch_steam(profile: GameProfile) -> int:
    adapter = get_asar_adapter(profile)
    PatchManager().patch(profile, adapter)
    resolved = resolve_profile(profile)
    argv = [
        str(resolved.wine),
        "--bottle",
        profile.bottle,
        "--no-wait",
        str(resolved.steam_exe),
        "-applaunch",
        str(profile.steam.app_id),
        "--crossover-browser",
    ]
    subprocess.Popen(argv, shell=False)
    return 0


def _launch(profile: GameProfile, *, no_open: bool) -> int:
    # Doctor is deliberately blocking for both modes.  This keeps an invalid
    # bottle/asset setup from triggering patching or opening a second listener.
    if _run_doctor(profile):
        return 1
    if profile.mode == "browser-only":
        return _launch_browser(profile, no_open=no_open)
    if profile.mode == "steam-electron":
        return _launch_steam(profile)
    # Profiles are strictly validated, but retain a closed command boundary if
    # a caller injects a malformed GameProfile in a test.
    raise ConfigurationError(f"unsupported profile mode {profile.mode}")


def _patch(profile: GameProfile) -> int:
    if profile.mode != "steam-electron":
        raise ConfigurationError(
            f"patch is unavailable for {profile.mode} profile {profile.id}"
        )
    adapter = get_asar_adapter(profile)
    PatchManager().patch(profile, adapter)
    return 0


def _restore(profile: GameProfile) -> int:
    if profile.mode != "steam-electron":
        raise ConfigurationError(
            f"restore is unavailable for {profile.mode} profile {profile.id}"
        )
    PatchManager().restore(profile)
    return 0


def _dispatch(args: argparse.Namespace) -> int:
    if args.command == "profiles":
        return _profiles()

    profile = _load_named_profile(args.profile, args.profile_dir)
    if args.command == "doctor":
        return _run_doctor(profile)
    if args.command == "launch":
        return _launch(profile, no_open=args.no_open)
    if args.command == "patch":
        return _patch(profile)
    if args.command == "restore":
        return _restore(profile)
    raise ConfigurationError(f"unknown command {args.command}")


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI and return its process status without raising ``SystemExit``."""

    parser = _parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        # argparse has already emitted its help/error text to the proper stream.
        return int(exc.code) if isinstance(exc.code, int) else 2

    try:
        return _dispatch(args)
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        # Runtime failures (missing files, unavailable ports, failed patching,
        # and process launch errors) are operational and must not be mistaken
        # for malformed command/profile input.
        print(f"error: {exc}", file=sys.stderr)
        return 1


__all__ = ["BUILTIN_PROFILE_IDS", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
