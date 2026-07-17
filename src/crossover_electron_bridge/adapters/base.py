"""Protocols shared by code-registered game adapters.

The concrete adapters are intentionally kept out of this module.  They are
loaded by :mod:`crossover_electron_bridge.adapters` only when a caller asks
for a particular adapter, so importing the package never imports optional
stage-specific implementations.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..patching import RuntimeResources
    from ..profiles import GameProfile


@runtime_checkable
class AsarPatchAdapter(Protocol):
    """Code implementation for patching one supported Electron archive."""

    revision: str

    def apply(
        self,
        extracted_root: Path,
        profile: GameProfile,
        resources: RuntimeResources,
    ) -> None:
        """Apply this adapter's deterministic changes in-place."""


__all__ = ["AsarPatchAdapter"]
