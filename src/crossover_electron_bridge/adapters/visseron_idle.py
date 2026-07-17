"""Visseron Idle persistence and browser bridge adapter.

The adapter contains the state-management part of the original Visseron
launcher, while :mod:`crossover_electron_bridge.browser` owns the HTTP
listener, authentication, static files, and request framing.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from ..atomic import atomic_write_text
from ..paths import bottle_root
from ..browser import ApiRequest, ApiResponse, BrowserAdapter


_DEFAULT_SAVE_DIR = "~/.visseron-idle"
_DEFAULT_WINE_STATE_PATH = "users/crossover/AppData/Roaming/visseron-idle"
_DEFAULT_MAX_REQUEST_BYTES = 4 * 1024 * 1024
_DEFAULT_MAX_BACKUPS = 10
_DEFAULT_BACKUP_INTERVAL_SECONDS = 300
_DEFAULT_BACKUP_SLOTS = (1, 2, 3)


def _reject_json_constant(_value: str) -> None:
    raise ValueError("non-standard JSON number")


def _validate_json(json_string: str) -> None:
    """Raise ``ValueError`` unless *json_string* is one complete strict JSON value."""

    try:
        json.loads(json_string, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, RecursionError, UnicodeError, ValueError) as exc:
        raise ValueError("invalid JSON") from exc


def _expand_home(value: str, home: Path) -> Path:
    """Expand only the configured home, rather than the process HOME."""

    if value == "~":
        return home
    if value.startswith("~/"):
        return home / value[2:]
    return Path(value)


def _relative_wine_path(value: str) -> Path:
    """Validate a relative Wine path before joining it to the bottle root."""

    path = PurePosixPath(value)
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("wine_state_path must be a safe relative path")
    if "\\" in value:
        raise ValueError("wine_state_path must use POSIX separators")
    return Path(*path.parts)


def _config_value(config: Mapping[str, Any], name: str, default: Any) -> Any:
    value = config.get(name, default)
    # Profile data is validated/frozen by profiles.py, but callers may build a
    # GameProfile directly in tests.  Refuse mutable or malformed values here.
    return value


class VisseronAdapter(BrowserAdapter):
    """Persistence adapter for the browser-only Visseron Idle profile."""

    id = "visseron_idle"

    def __init__(
        self,
        profile: Any,
        home: Path = Path.home(),
        now: Callable[[], float] | None = None,
        *,
        clock: Callable[[], float] | Any | None = None,
    ) -> None:
        self.profile = profile
        self.home = Path(home)
        if getattr(profile, "mode", None) != "browser-only":
            raise ValueError("VisseronAdapter requires browser-only mode")
        config = getattr(profile, "adapter_config", {}) or {}

        save_dir_value = _config_value(config, "save_dir", _DEFAULT_SAVE_DIR)
        if not isinstance(save_dir_value, str) or not save_dir_value:
            raise ValueError("save_dir must be a non-empty string")
        self.save_dir = _expand_home(save_dir_value, self.home)
        self.save_file = self.save_dir / "save-data.json"
        self.manual_save_file = self.save_dir / "save-manual.json"
        self.backup_dir = self.save_dir / "backups"

        wine_path_value = _config_value(
            config, "wine_state_path", _DEFAULT_WINE_STATE_PATH
        )
        if not isinstance(wine_path_value, str) or not wine_path_value:
            raise ValueError("wine_state_path must be a non-empty string")
        wine_path = _expand_home(wine_path_value, self.home)
        if not wine_path.is_absolute():
            wine_path = bottle_root(str(profile.bottle), self.home) / "drive_c" / _relative_wine_path(wine_path_value)
        self.wine_save_dir = wine_path

        self.max_request_bytes = self._positive_int(
            _config_value(config, "max_request_bytes", _DEFAULT_MAX_REQUEST_BYTES),
            "max_request_bytes",
        )
        self.max_backups = self._nonnegative_int(
            _config_value(config, "max_backups", _DEFAULT_MAX_BACKUPS),
            "max_backups",
        )
        self.backup_interval_seconds = self._nonnegative_number(
            _config_value(
                config, "backup_interval_seconds", _DEFAULT_BACKUP_INTERVAL_SECONDS
            ),
            "backup_interval_seconds",
        )
        slots = _config_value(config, "backup_slots", _DEFAULT_BACKUP_SLOTS)
        if isinstance(slots, (str, bytes)):
            raise ValueError("backup_slots must be an array of positive integers")
        try:
            slots_tuple = tuple(slots)
        except TypeError as exc:
            raise ValueError("backup_slots must be an array of positive integers") from exc
        if not slots_tuple or any(
            isinstance(slot, bool) or not isinstance(slot, int) or slot <= 0
            for slot in slots_tuple
        ):
            raise ValueError("backup_slots must be an array of positive integers")
        if len(set(slots_tuple)) != len(slots_tuple):
            raise ValueError("backup_slots must not contain duplicates")
        self.backup_slots = tuple(sorted(slots_tuple))

        if now is not None and clock is not None:
            raise TypeError("provide now or clock, not both")
        selected_clock = now if now is not None else clock
        if selected_clock is None:
            self._now: Callable[[], float] = time.time
        elif callable(selected_clock):
            self._now = selected_clock
        elif callable(getattr(selected_clock, "time", None)):
            self._now = selected_clock.time
        elif callable(getattr(selected_clock, "now", None)):
            self._now = selected_clock.now
        elif isinstance(selected_clock, (int, float)) and not isinstance(selected_clock, bool):
            self._now = lambda: float(selected_clock)
        else:
            raise TypeError("now/clock must be callable, numeric, or expose time()/now()")
        # ``None`` makes a synthetic clock at zero behave naturally on its
        # first backup; wall-clock operation is equivalent to the old 0 epoch
        # sentinel for all practical timestamps.
        self._last_backup_time: float | None = None

    @staticmethod
    def _positive_int(value: Any, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
        return value

    @staticmethod
    def _nonnegative_int(value: Any, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
        return value

    @staticmethod
    def _nonnegative_number(value: Any, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"{name} must be a non-negative number")
        return float(value)

    def before_start(self) -> None:
        """Create the private state directories before the server starts."""

        self.save_dir.mkdir(parents=True, exist_ok=True)
        self.backup_dir.mkdir(parents=True, exist_ok=True)

    def bridge_script(self, token: str) -> str:
        """Return the token-bound ``window.electronAPI`` browser shim."""

        return SHIM_JS.replace("__CROSSOVER_TOKEN__", json.dumps(token))

    def _atomic_write(self, path: Path, data: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, data, mode=0o600)

    def load_save(self) -> str | None:
        """Load the native save, importing a valid Wine save on first run."""

        if self.save_file.exists():
            return self.save_file.read_text(encoding="utf-8")

        wine_save = self.wine_save_dir / "save-data.json"
        if not wine_save.exists():
            return None
        data = wine_save.read_text(encoding="utf-8")
        try:
            _validate_json(data)
        except ValueError:
            return None
        self._atomic_write(self.save_file, data)
        return data

    def _backup_name(self, timestamp: float) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(timestamp))
        candidate = self.backup_dir / f"save-{stamp}.json"
        suffix = 1
        while candidate.exists():
            candidate = self.backup_dir / f"save-{stamp}-{suffix}.json"
            suffix += 1
        return candidate

    def _preserve_prior_save(self) -> None:
        """Preserve the current valid generation at most once per interval."""

        if not self.save_file.exists():
            return
        try:
            previous = self.save_file.read_text(encoding="utf-8")
            _validate_json(previous)
        except (OSError, UnicodeError, ValueError):
            return

        now = float(self._now())
        if (
            self._last_backup_time is not None
            and now - self._last_backup_time < self.backup_interval_seconds
        ):
            return
        backup = self._backup_name(now)
        self._atomic_write(backup, previous)
        self._last_backup_time = now

        backups = sorted(self.backup_dir.glob("save-*.json"))
        if len(backups) > self.max_backups:
            for old in backups[: len(backups) - self.max_backups]:
                old.unlink(missing_ok=True)

    def _replace_main_save(self, json_string: str) -> None:
        _validate_json(json_string)
        self._preserve_prior_save()
        self._atomic_write(self.save_file, json_string)

    def write_save(self, json_string: str) -> dict[str, Any]:
        self._replace_main_save(json_string)
        return {"success": True}

    def write_manual_save(self, json_string: str) -> dict[str, Any]:
        _validate_json(json_string)
        self._atomic_write(self.manual_save_file, json_string)
        return {"success": True}

    def load_backups(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for slot in self.backup_slots:
            backup = self.wine_save_dir / f"save-backup-{slot}.json"
            entry: dict[str, Any] = {
                "slot": str(slot),
                "label": f"Backup {slot}",
                "savedAt": None,
                "totalPlayTime": None,
            }
            if backup.exists():
                try:
                    data = json.loads(backup.read_text(encoding="utf-8"))
                    entry["savedAt"] = data.get("savedAt")
                    entry["totalPlayTime"] = data.get("totalPlayTime")
                except (json.JSONDecodeError, OSError, UnicodeError, AttributeError):
                    pass
            result.append(entry)
        return result

    def restore_backup(self, slot: int) -> dict[str, Any]:
        if isinstance(slot, bool) or not isinstance(slot, int) or slot not in self.backup_slots:
            return {"success": False, "error": "Invalid backup slot"}

        backup = self.wine_save_dir / f"save-backup-{slot}.json"
        if not backup.exists():
            return {"success": False, "error": f"Backup {slot} not found"}
        try:
            data = backup.read_text(encoding="utf-8")
            _validate_json(data)
        except (OSError, UnicodeError, ValueError):
            return {"success": False, "error": f"Backup {slot} is invalid"}

        self._replace_main_save(data)
        return {"success": True}

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str | None:
        wanted = name.lower()
        for key, value in headers.items():
            if str(key).lower() == wanted:
                return str(value)
        return None

    @staticmethod
    def _response(status: int, body: Any) -> ApiResponse:
        return ApiResponse(status, body)

    def _json_body(self, body: bytes) -> str | None:
        if len(body) > self.max_request_bytes:
            return None
        try:
            json_string = body.decode("utf-8")
            _validate_json(json_string)
        except (UnicodeDecodeError, ValueError):
            return None
        return json_string

    def handle_api(self, request: ApiRequest) -> ApiResponse | None:
        """Handle one authenticated API request; return ``None`` if unknown."""

        method = str(request.method).upper()
        path = request.path.split("?", 1)[0]
        known_methods = {
            "/api/load": "GET",
            "/api/backups": "GET",
            "/api/save": "POST",
            "/api/manual-save": "POST",
            "/api/delete": "POST",
            "/api/restore-backup": "POST",
        }
        expected = known_methods.get(path)
        if expected is None:
            return None
        if method != expected:
            return self._response(
                405, {"success": False, "error": "Method Not Allowed"}
            )

        body = bytes(request.body)
        if len(body) > self.max_request_bytes:
            return self._response(
                413, {"success": False, "error": "Request body too large"}
            )

        if path == "/api/load":
            try:
                return self._response(200, {"save": self.load_save()})
            except (OSError, UnicodeError):
                return self._response(
                    500, {"success": False, "error": "Unable to load save"}
                )
        if path == "/api/backups":
            return self._response(200, self.load_backups())

        if path in {"/api/save", "/api/manual-save"}:
            content_type = self._header(request.headers, "Content-Type") or ""
            media_type = content_type.split(";", 1)[0].strip().lower()
            if media_type != "application/json":
                return self._response(
                    415,
                    {
                        "success": False,
                        "error": "Content-Type must be application/json",
                    },
                )
            json_string = self._json_body(body)
            if json_string is None:
                return self._response(
                    400,
                    {
                        "success": False,
                        "error": "Request body must be valid JSON",
                    },
                )
            try:
                result = (
                    self.write_save(json_string)
                    if path == "/api/save"
                    else self.write_manual_save(json_string)
                )
            except OSError:
                return self._response(
                    500, {"success": False, "error": "Unable to persist save"}
                )
            return self._response(200, result)

        if path == "/api/delete":
            try:
                self.save_file.unlink(missing_ok=True)
            except OSError:
                return self._response(
                    500, {"success": False, "error": "Unable to delete save"}
                )
            return self._response(200, {"success": True})

        # Restore accepts only the decimal slot text used by the legacy shim;
        # constructing the filename from an arbitrary request string is never
        # allowed, which also makes traversal-shaped names harmless.
        try:
            slot_text = body.decode("ascii").strip()
        except UnicodeDecodeError:
            slot_text = ""
        if slot_text not in {str(slot) for slot in self.backup_slots}:
            return self._response(
                400, {"success": False, "error": "Invalid backup slot"}
            )
        try:
            result = self.restore_backup(int(slot_text))
        except OSError:
            return self._response(
                500, {"success": False, "error": "Unable to restore backup"}
            )
        return self._response(200, result)


SHIM_JS = r"""
const crossoverToken = __CROSSOVER_TOKEN__;

const apiFetch = (url, options = {}) => {
  const headers = new Headers(options.headers || {});
  headers.set('X-Crossover-Token', crossoverToken);
  return fetch(url, { ...options, headers });
};

window.electronAPI = {
  isElectron: true,

  // ── Save / Load (HTTP-backed) ──────────────────────────────────────────
  save: async (jsonString) => {
    const r = await apiFetch('/api/save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: jsonString,
    });
    return r.json();
  },
  load: async () => {
    const r = await apiFetch('/api/load');
    const data = await r.json();
    return data.save; // string or null
  },
  deleteSave: async () => {
    await apiFetch('/api/delete', { method: 'POST' });
    return { success: true };
  },
  getSavePath: async () => "~/.visseron-idle/save-data.json",
  getLogPath: async () => null,

  // ── Window lifecycle (no-ops in browser) ──────────────────────────────
  onClosing: (cb) => { window._visseronOnClose = cb; },
  confirmClose: () => {},
  enableAutoSave: async () => {},

  // ── Backups ───────────────────────────────────────────────────────────
  loadBackups: async () => {
    const r = await apiFetch('/api/backups');
    return r.json();
  },
  restoreBackup: async (slot) => {
    const r = await apiFetch('/api/restore-backup', { method: 'POST', body: String(slot) });
    return r.json();
  },
  requestCloseRotation: async () => {},

  // ── Manual save ───────────────────────────────────────────────────────
  manualSave: async (jsonString) => {
    const r = await apiFetch('/api/manual-save', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: jsonString,
    });
    return r.json();
  },

  // ── Steam (stubbed — not available in browser) ────────────────────────
  steam: {
    isAvailable: async () => false,
    getPlayerName: async () => null,
    unlockAchievement: async () => false,
    getAchievement: async () => null,
    activateOverlay: async () => {},
    getCloudSaveStatus: async () => ({ enabled: false, hasCloudSave: false }),
  },

  // ── Avatar (stubbed — not available in browser) ────────────────────────
  avatar: {
    pick: async () => null,
    save: async () => ({ success: false, error: 'Not available in browser' }),
    load: async () => null,
    delete: async () => ({ success: false, error: 'Not available in browser' }),
  },
};
"""


__all__ = ["VisseronAdapter"]
