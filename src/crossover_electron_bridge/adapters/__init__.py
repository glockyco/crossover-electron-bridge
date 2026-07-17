"""Fixed, code-registered adapter lookup.

Profile data contains adapter *identifiers* only.  This module maps those
identifiers to implementations in package-owned modules; it never imports a
module path supplied by a profile.  Imports are inside the lookup functions so
staged installations can import the package before every adapter module has
been added.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ..errors import ConfigurationError
from .base import AsarPatchAdapter

if TYPE_CHECKING:
    from ..browser import BrowserAdapter
    from ..profiles import GameProfile


# Keep this set deliberately closed.  It mirrors the profile validator's
# allowed identifiers, but the registry owns the implementation boundary.
ADAPTER_IDS = frozenset({"fractured_realms", "visseron_idle", "static"})
# Public compatibility with the name used by profile validation and callers
# that need to display the fixed registry.
REGISTERED_ADAPTERS = ADAPTER_IDS
_MODES = frozenset({"browser-only", "steam-electron"})


def is_registered(adapter_id: str) -> bool:
    """Return whether *adapter_id* is one of the fixed code IDs."""

    return isinstance(adapter_id, str) and adapter_id in ADAPTER_IDS


def _details(profile_or_id: Any, mode: str | None) -> tuple[str, str, Any]:
    """Extract a validated registry ID and mode without importing profiles."""

    if isinstance(profile_or_id, str):
        adapter_id = profile_or_id
        profile = None
        profile_mode = mode
    else:
        profile = profile_or_id
        adapter_id = getattr(profile, "adapter", None)
        profile_mode = getattr(profile, "mode", None)
        if mode is not None and profile_mode != mode:
            raise ConfigurationError("adapter mode does not match profile mode")

    if not isinstance(adapter_id, str) or not is_registered(adapter_id):
        raise ConfigurationError(f"unknown adapter {adapter_id}")
    if not isinstance(profile_mode, str) or profile_mode not in _MODES:
        raise ConfigurationError(f"invalid adapter mode {profile_mode}")
    return adapter_id, profile_mode, profile


def get_asar_adapter(
    profile_or_id: GameProfile | str,
    mode: str | None = None,
) -> AsarPatchAdapter:
    """Return the fixed ASAR patch adapter for a steam-electron profile.

    ``profile_or_id`` normally is a :class:`GameProfile`.  The explicit
    ``(adapter_id, mode)`` form is also useful to callers that have already
    selected a profile and keeps the mode restriction impossible to omit.
    """

    adapter_id, profile_mode, _profile = _details(profile_or_id, mode)
    if adapter_id == "static":
        raise ConfigurationError("adapter static has no ASAR patch implementation")
    if profile_mode != "steam-electron":
        raise ConfigurationError("ASAR patch adapters require steam-electron mode")
    if adapter_id != "fractured_realms":
        raise ConfigurationError(
            f"adapter {adapter_id} has no ASAR patch implementation"
        )

    # Deliberately lazy: this module may be imported while an earlier phase is
    # being installed and before the concrete adapter module exists.
    from .fractured_realms import FracturedRealmsAdapter

    return FracturedRealmsAdapter()


def get_browser_adapter(profile: GameProfile) -> BrowserAdapter:
    """Return the fixed Python browser adapter for a browser-only profile."""

    adapter_id, profile_mode, _ = _details(profile, None)
    if profile_mode != "browser-only":
        raise ConfigurationError("browser adapters require browser-only mode")

    if adapter_id == "static":
        # StaticAdapter lives with the shared browser host, not in a profile
        # selected module.  Import it only when this lookup is requested.
        from ..browser import StaticAdapter

        return StaticAdapter()
    if adapter_id == "visseron_idle":
        from .visseron_idle import VisseronAdapter

        return VisseronAdapter(profile)
    raise ConfigurationError(
        f"adapter {adapter_id} has no Python browser implementation"
    )


__all__ = [
    "ADAPTER_IDS",
    "AsarPatchAdapter",
    "REGISTERED_ADAPTERS",
    "get_asar_adapter",
    "get_browser_adapter",
    "is_registered",
]
