#!/usr/bin/env python3
"""
Visseron Idle — Local Web Server
================================

Serves the game's Vite SPA from the Steam install directory, with a shim
that replaces Electron's IPC with HTTP calls back to this server for
save/load. Saves are stored on disk in ~/.visseron-idle/ so they survive
browser data clearing and work across any browser.

On first run, automatically imports the latest save from the Wine bottle.

Usage:
  python3 server.py              # start + open browser
  python3 server.py --no-open    # start without opening browser

Launch Visseron Idle.command from this project directory to start it.
"""

import http.server
import json
import os
import secrets
import sys
import tempfile
import time
import urllib.parse
import webbrowser
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────

PORT = 8765
DIST_DIR = Path.home() / (
    "Library/Application Support/CrossOver/Bottles/Steam/drive_c/"
    "Program Files (x86)/Steam/steamapps/common/Visseron Idle/"
    "resources/app/dist"
)
WINE_SAVE_DIR = Path.home() / (
    "Library/Application Support/CrossOver/Bottles/Steam/drive_c/"
    "users/crossover/AppData/Roaming/visseron-idle"
)
SAVE_DIR = Path.home() / ".visseron-idle"
SAVE_FILE = SAVE_DIR / "save-data.json"
MANUAL_SAVE_FILE = SAVE_DIR / "save-manual.json"
BACKUP_DIR = SAVE_DIR / "backups"
MAX_BACKUPS = 10
BACKUP_INTERVAL_S = 300  # 5 minutes
MAX_REQUEST_BYTES = 4 * 1024 * 1024  # Keep one profile save bounded at 4 MiB.
BACKUP_SLOTS = frozenset({1, 2, 3})
ALLOWED_HOSTS = frozenset({f"localhost:{PORT}", f"127.0.0.1:{PORT}"})
ALLOWED_ORIGINS = frozenset({f"http://{host}" for host in ALLOWED_HOSTS})
PROCESS_TOKEN = secrets.token_urlsafe(32)

SAVE_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)


# ── Save management ──────────────────────────────────────────────────────────

def _reject_json_constant(_value):
    raise ValueError("non-standard JSON number")


def _validate_json(json_string: str) -> None:
    """Raise ValueError unless json_string is a complete, strict JSON value."""
    try:
        json.loads(json_string, parse_constant=_reject_json_constant)
    except (json.JSONDecodeError, RecursionError, UnicodeError, ValueError) as exc:
        raise ValueError("invalid JSON") from exc


def _atomic_write(path: Path, data: str) -> None:
    """Write data through a private, fsynced temp file and atomically replace path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    open_fd = True
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            open_fd = False
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if open_fd:
            os.close(fd)
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def load_save() -> str | None:
    """Load the main save, importing from Wine on first run."""
    if SAVE_FILE.exists():
        return SAVE_FILE.read_text("utf-8")

    # First run — try to import from the Wine bottle.
    wine_save = WINE_SAVE_DIR / "save-data.json"
    if wine_save.exists():
        data = wine_save.read_text("utf-8")
        try:
            _validate_json(data)
        except ValueError:
            return None
        _atomic_write(SAVE_FILE, data)
        print(f"  Imported save from Wine ({len(data)} bytes)")
        return data

    return None


_last_backup_time = 0.0


def _preserve_prior_save() -> None:
    """Back up the current valid generation before replacing the main save."""
    global _last_backup_time

    if not SAVE_FILE.exists():
        return
    try:
        previous = SAVE_FILE.read_text("utf-8")
        _validate_json(previous)
    except (OSError, UnicodeError, ValueError):
        return

    now = time.time()
    if now - _last_backup_time < BACKUP_INTERVAL_S:
        return

    ts = time.strftime("%Y%m%d-%H%M%S")
    backup = BACKUP_DIR / f"save-{ts}.json"
    _atomic_write(backup, previous)
    _last_backup_time = now

    # Prune old backups, keep most recent MAX_BACKUPS.
    backups = sorted(BACKUP_DIR.glob("save-*.json"))
    if len(backups) > MAX_BACKUPS:
        for old in backups[:len(backups) - MAX_BACKUPS]:
            old.unlink(missing_ok=True)


def _replace_main_save(json_string: str) -> None:
    _validate_json(json_string)
    _preserve_prior_save()
    _atomic_write(SAVE_FILE, json_string)


def write_save(json_string: str) -> dict:
    _replace_main_save(json_string)
    return {"success": True}


def write_manual_save(json_string: str) -> dict:
    _validate_json(json_string)
    _atomic_write(MANUAL_SAVE_FILE, json_string)
    return {"success": True}


def load_backups() -> list:
    """Return metadata for all backup slots."""
    result = []
    for i in sorted(BACKUP_SLOTS):
        backup = WINE_SAVE_DIR / f"save-backup-{i}.json"
        entry = {"slot": str(i), "label": f"Backup {i}", "savedAt": None, "totalPlayTime": None}
        if backup.exists():
            try:
                data = json.loads(backup.read_text("utf-8"))
                entry["savedAt"] = data.get("savedAt")
                entry["totalPlayTime"] = data.get("totalPlayTime")
            except (json.JSONDecodeError, OSError, UnicodeError, AttributeError):
                pass
        result.append(entry)
    return result


def restore_backup(slot: int) -> dict:
    if isinstance(slot, bool) or not isinstance(slot, int) or slot not in BACKUP_SLOTS:
        return {"success": False, "error": "Invalid backup slot"}

    backup = WINE_SAVE_DIR / f"save-backup-{slot}.json"
    if not backup.exists():
        return {"success": False, "error": f"Backup {slot} not found"}
    try:
        data = backup.read_text("utf-8")
        _validate_json(data)
    except (OSError, UnicodeError, ValueError):
        return {"success": False, "error": f"Backup {slot} is invalid"}

    _replace_main_save(data)
    return {"success": True}


# ── electronAPI shim ────────────────────────────────────────────────────────

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


# ── HTTP handler ─────────────────────────────────────────────────────────────

class VisseronHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DIST_DIR), **kwargs)

    def _authorize_api(self) -> bool:
        host = self.headers.get("Host")
        if host not in ALLOWED_HOSTS:
            self._json({"success": False, "error": "Invalid Host"}, status=403)
            return False

        origin = self.headers.get("Origin")
        if origin is not None and (origin not in ALLOWED_ORIGINS or origin.removeprefix("http://") != host):
            self._json({"success": False, "error": "Invalid Origin"}, status=403)
            return False

        presented = self.headers.get("X-Crossover-Token", "")
        if not secrets.compare_digest(presented, PROCESS_TOKEN):
            self._json({"success": False, "error": "Unauthorized"}, status=401)
            return False
        return True

    def _read_body(self) -> bytes | None:
        transfer_encoding = self.headers.get("Transfer-Encoding")
        if transfer_encoding and transfer_encoding.lower() != "identity":
            self._json({"success": False, "error": "Unsupported Transfer-Encoding"}, status=400)
            return None

        length_header = self.headers.get("Content-Length")
        if length_header is None:
            return b""
        try:
            length = int(length_header)
        except ValueError:
            self._json({"success": False, "error": "Invalid Content-Length"}, status=400)
            return None
        if length < 0:
            self._json({"success": False, "error": "Invalid Content-Length"}, status=400)
            return None
        if length > MAX_REQUEST_BYTES:
            self._json({"success": False, "error": "Request body too large"}, status=413)
            return None

        body = bytearray()
        remaining = length
        while remaining:
            try:
                chunk = self.rfile.read(min(64 * 1024, remaining))
            except OSError:
                self._json({"success": False, "error": "Unable to read request body"}, status=400)
                return None
            if not chunk:
                self._json({"success": False, "error": "Incomplete request body"}, status=400)
                return None
            body.extend(chunk)
            remaining -= len(chunk)
        return bytes(body)

    def _read_json_body(self, body: bytes) -> str | None:
        try:
            json_string = body.decode("utf-8")
            _validate_json(json_string)
        except (UnicodeDecodeError, ValueError):
            self._json({"success": False, "error": "Request body must be valid JSON"}, status=400)
            return None
        return json_string

    def _require_json_content(self) -> bool:
        content_type = self.headers.get("Content-Type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            self._json({"success": False, "error": "Content-Type must be application/json"}, status=415)
            return False
        return True

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)

        if parsed.path.startswith("/api/") and not self._authorize_api():
            return
        if parsed.path in ("/", "/index.html"):
            self._serve_index()
        elif parsed.path == "/api/load":
            save = load_save()
            self._json({"save": save})
        elif parsed.path == "/api/backups":
            self._json(load_backups())
        else:
            super().do_GET()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path.startswith("/api/") and not self._authorize_api():
            return

        if parsed.path not in {"/api/save", "/api/manual-save", "/api/delete", "/api/restore-backup"}:
            self.send_error(404)
            return

        if parsed.path in {"/api/save", "/api/manual-save"} and not self._require_json_content():
            return
        body = self._read_body()
        if body is None:
            return

        if parsed.path in {"/api/save", "/api/manual-save"}:
            json_string = self._read_json_body(body)
            if json_string is None:
                return
            try:
                result = write_save(json_string) if parsed.path == "/api/save" else write_manual_save(json_string)
            except OSError:
                self._json({"success": False, "error": "Unable to persist save"}, status=500)
                return
            self._json(result)
        elif parsed.path == "/api/delete":
            try:
                SAVE_FILE.unlink(missing_ok=True)
            except OSError:
                self._json({"success": False, "error": "Unable to delete save"}, status=500)
                return
            self._json({"success": True})
        else:
            try:
                slot_text = body.decode("ascii").strip()
            except UnicodeDecodeError:
                slot_text = ""
            if slot_text not in {str(slot) for slot in BACKUP_SLOTS}:
                self._json({"success": False, "error": "Invalid backup slot"}, status=400)
                return
            try:
                result = restore_backup(int(slot_text))
            except OSError:
                self._json({"success": False, "error": "Unable to restore backup"}, status=500)
                return
            self._json(result)

    def _serve_index(self):
        try:
            html = (DIST_DIR / "index.html").read_text("utf-8")
        except FileNotFoundError:
            self.send_error(404, "index.html not found — is the game installed in Steam?")
            return

        shim_source = SHIM_JS.replace("__CROSSOVER_TOKEN__", json.dumps(PROCESS_TOKEN))
        shim = f"<script>{shim_source}</script>\n"
        html = html.replace("<script ", f"{shim}<script ", 1)
        content = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _json(self, obj, status: int = 200):
        content = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *args):
        pass  # quiet


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    auto_open = "--no-open" not in sys.argv

    if not DIST_DIR.exists():
        print(f"\n  ✗ Game not found at:\n    {DIST_DIR}")
        print(f"  Make sure Visseron Idle is installed in the CrossOver Steam bottle.\n")
        sys.exit(1)

    server = http.server.HTTPServer(("127.0.0.1", PORT), VisseronHandler)

    # Pre-load the save to trigger Wine import if needed.
    existing = load_save()
    if existing:
        print(f"  Save ready ({len(existing)} bytes)")
    else:
        print("  No save found — starting fresh.")

    url = f"http://localhost:{PORT}"
    print(f"\n  ▶ Visseron Idle:  {url}")
    print(f"  Saves stored in:  {SAVE_DIR}")
    print(f"  Press Ctrl+C to stop.\n")

    if auto_open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  Stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
