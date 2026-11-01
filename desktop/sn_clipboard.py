#!/usr/bin/env python3
"""Supernote Clipboard - a screenshot bridge for the "PC Clipboard" Supernote plugin.

Take a screenshot on your computer, then tap "Paste screenshot" on your Supernote
and the image is inserted into the note you are writing in.

Screenshots enter the app in three ways:
  * region capture: the global shortcut (Option+Shift+S on macOS, Ctrl+Alt+S on
    Windows), the "Capture region" button, or `sn_clipboard.py capture`
  * any screenshot/image copied to the system clipboard (auto-import)
  * drag & drop or Ctrl/Cmd+V into the web UI

The Supernote finds this app on your Wi-Fi network by itself. The first time a
Supernote connects, the web UI asks you to allow it; after that it is paired and
only paired devices can download screenshots. The web UI itself only answers on
this computer (localhost).

Only the Python 3.8+ standard library is used.
"""

import argparse
import ctypes
import ctypes.util
import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_NAME = "Supernote Clipboard"
VERSION = "1.1.0"
DEFAULT_PORT = 8765
MAX_SHOTS = 30
MAX_UPLOAD_BYTES = 40 * 1024 * 1024
PAIR_REQUEST_TTL = 120
MAX_PENDING_PAIRS = 5
SYSTEM = platform.system()
DEFAULT_HOTKEY = {"Darwin": "alt+shift+s", "Windows": "ctrl+alt+s"}.get(SYSTEM, "")
DATA_DIR = Path(os.environ.get("SN_CLIPBOARD_HOME", str(Path.home() / ".sn-clipboard")))
SHOT_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
WIN_NO_WINDOW = 0x08000000


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

class Config:
    def __init__(self, path):
        self.path = path
        self.server_id = ""
        self.port = DEFAULT_PORT
        self.watch_clipboard = True
        self.hotkey = DEFAULT_HOTKEY
        self.devices = []  # [{"id", "name", "token", "pairedAt"}]
        self.lock = threading.RLock()
        self._load()

    def _load(self):
        data = {}
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text("utf-8"))
            except (OSError, ValueError):
                data = {}
        self.server_id = str(data.get("server_id") or secrets.token_hex(8))
        self.port = int(data.get("port") or DEFAULT_PORT)
        self.watch_clipboard = bool(data.get("watch_clipboard", True))
        self.hotkey = str(data.get("hotkey", DEFAULT_HOTKEY))
        self.devices = [d for d in data.get("devices", []) if isinstance(d, dict) and d.get("token")]
        self.save()

    def save(self):
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "server_id": self.server_id,
                "port": self.port,
                "watch_clipboard": self.watch_clipboard,
                "hotkey": self.hotkey,
                "devices": self.devices,
            }
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), "utf-8")
            tmp.replace(self.path)

    def device_for_token(self, token):
        if not token:
            return None
        with self.lock:
            for device in self.devices:
                if hmac.compare_digest(device["token"].encode(), token.encode()):
                    return device
        return None

    def add_device(self, name):
        device = {
            "id": secrets.token_hex(4),
            "name": name,
            "token": secrets.token_urlsafe(32),
            "pairedAt": int(time.time() * 1000),
        }
        with self.lock:
            self.devices.append(device)
            self.save()
        return device

    def remove_device(self, device_id):
        with self.lock:
            before = len(self.devices)
            self.devices = [d for d in self.devices if d["id"] != device_id]
            self.save()
            return len(self.devices) != before


# --------------------------------------------------------------------------- #
# Image helpers
# --------------------------------------------------------------------------- #

def image_info(data):
    """Return (ext, width, height) for PNG/JPEG bytes, or None."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        width, height = struct.unpack(">II", data[16:24])
        return "png", width, height
    if data[:2] == b"\xff\xd8":
        size = _jpeg_size(data)
        if size:
            return "jpg", size[0], size[1]
    return None


def _jpeg_size(data):
    sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF:
            i += 1
            continue
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in sof_markers:
            height, width = struct.unpack(">HH", data[i + 5:i + 9])
            return width, height
        i += 2 + seg_len
    return None


def _ps_quote(path):
    return "'" + str(path).replace("'", "''") + "'"


def run_powershell(script, timeout=30):
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-STA", "-Command", script],
        capture_output=True, timeout=timeout, creationflags=WIN_NO_WINDOW,
    )


def resize_image(src, dst, max_width):
    """Scale src down to max_width pixels wide. Returns True when dst was written."""
    try:
        if SYSTEM == "Darwin":
            subprocess.run(
                ["sips", "--resampleWidth", str(max_width), str(src), "--out", str(dst)],
                capture_output=True, timeout=30,
            )
        elif SYSTEM == "Windows":
            fmt = "Png" if dst.suffix == ".png" else "Jpeg"
            run_powershell(f"""
Add-Type -AssemblyName System.Drawing
$src = [System.Drawing.Image]::FromFile({_ps_quote(src)})
$w = {int(max_width)}
$h = [int][Math]::Round($src.Height * $w / $src.Width)
$bmp = New-Object System.Drawing.Bitmap $w, $h
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
$g.DrawImage($src, 0, 0, $w, $h)
$g.Dispose(); $src.Dispose()
$bmp.Save({_ps_quote(dst)}, [System.Drawing.Imaging.ImageFormat]::{fmt})
$bmp.Dispose()
""")
        else:
            tool = shutil.which("magick") or shutil.which("convert")
            if not tool:
                return False
            subprocess.run([tool, str(src), "-resize", f"{int(max_width)}x", str(dst)],
                           capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return dst.exists() and dst.stat().st_size > 0


# --------------------------------------------------------------------------- #
# Screenshot store
# --------------------------------------------------------------------------- #

class ShotStore:
    def __init__(self, directory, limit=MAX_SHOTS):
        self.dir = directory
        self.cache_dir = directory / "cache"
        self.index_path = directory / "index.json"
        self.limit = limit
        self.lock = threading.RLock()
        self.shots = []
        self.revision = 0
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(exist_ok=True)
        self._load()

    def _load(self):
        if not self.index_path.exists():
            return
        try:
            entries = json.loads(self.index_path.read_text("utf-8"))
        except (OSError, ValueError):
            return
        for entry in entries:
            if SHOT_ID_RE.match(str(entry.get("id", ""))) and self._file(entry).exists():
                self.shots.append(entry)

    def _save(self):
        self.revision += 1
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.shots, indent=1), "utf-8")
        tmp.replace(self.index_path)

    def _file(self, shot):
        return self.dir / f"{shot['id']}.{shot['ext']}"

    def _remove_files(self, shot):
        self._file(shot).unlink(missing_ok=True)
        for cached in self.cache_dir.glob(f"{shot['id']}_w*"):
            cached.unlink(missing_ok=True)

    def add(self, data, source):
        info = image_info(data)
        if not info:
            raise ValueError("Only PNG and JPEG images are supported.")
        ext, width, height = info
        digest = hashlib.sha1(data).hexdigest()
        with self.lock:
            for shot in self.shots:
                if shot["sha1"] == digest:
                    self.promote(shot["id"])
                    return shot
            shot = {
                "id": time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2),
                "ext": ext,
                "width": width,
                "height": height,
                "createdAt": int(time.time() * 1000),
                "source": source,
                "sha1": digest,
            }
            self._file(shot).write_bytes(data)
            self.shots.insert(0, shot)
            for old in self.shots[self.limit:]:
                self._remove_files(old)
            del self.shots[self.limit:]
            self._save()
        log(f"New screenshot {shot['id']} ({width}x{height}, from {source})")
        return shot

    def list(self):
        with self.lock:
            return [dict(s) for s in self.shots]

    def latest(self):
        with self.lock:
            return dict(self.shots[0]) if self.shots else None

    def get(self, shot_id):
        with self.lock:
            for shot in self.shots:
                if shot["id"] == shot_id:
                    return dict(shot)
        return None

    def promote(self, shot_id):
        with self.lock:
            for i, shot in enumerate(self.shots):
                if shot["id"] == shot_id:
                    self.shots.insert(0, self.shots.pop(i))
                    self._save()
                    return True
        return False

    def delete(self, shot_id):
        with self.lock:
            for i, shot in enumerate(self.shots):
                if shot["id"] == shot_id:
                    self._remove_files(shot)
                    del self.shots[i]
                    self._save()
                    return True
        return False

    def clear(self):
        with self.lock:
            for shot in self.shots:
                self._remove_files(shot)
            self.shots = []
            self._save()

    def read(self, shot, max_width=None):
        """Return image bytes, scaled down to max_width when the original is wider."""
        src = self._file(shot)
        if max_width and shot["width"] > max_width:
            cached = self.cache_dir / f"{shot['id']}_w{max_width}.{shot['ext']}"
            if cached.exists() or resize_image(src, cached, max_width):
                return cached.read_bytes()
        return src.read_bytes()


# --------------------------------------------------------------------------- #
# Clipboard access
# --------------------------------------------------------------------------- #

class MacClipboard:
    _EXPORT_SCRIPT = (
        'on run argv\n'
        '  set png to the clipboard as \u00abclass PNGf\u00bb\n'
        '  set fh to open for access (POSIX file (item 1 of argv)) with write permission\n'
        '  set eof fh to 0\n'
        '  write png to fh\n'
        '  close access fh\n'
        'end run\n'
    )

    def __init__(self):
        self._change_count = None
        try:
            objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
            ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AppKit.framework/AppKit")
            objc.objc_getClass.restype = ctypes.c_void_p
            objc.objc_getClass.argtypes = [ctypes.c_char_p]
            objc.sel_registerName.restype = ctypes.c_void_p
            objc.sel_registerName.argtypes = [ctypes.c_char_p]
            send_ptr = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(
                ("objc_msgSend", objc))
            send_long = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)(
                ("objc_msgSend", objc))
            board = send_ptr(objc.objc_getClass(b"NSPasteboard"),
                             objc.sel_registerName(b"generalPasteboard"))
            selector = objc.sel_registerName(b"changeCount")
            if board:
                self._change_count = lambda: send_long(board, selector)
        except (OSError, AttributeError, TypeError):
            self._change_count = None

    def change_token(self):
        if self._change_count:
            return self._change_count()
        out = subprocess.run(["osascript", "-e", "clipboard info"], capture_output=True, timeout=10)
        return out.stdout

    def read_image(self):
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            result = subprocess.run(["osascript", "-e", self._EXPORT_SCRIPT, path],
                                    capture_output=True, timeout=15)
            data = Path(path).read_bytes() if result.returncode == 0 else b""
            return data or None
        finally:
            os.unlink(path)


class WindowsClipboard:
    def __init__(self):
        self._user32 = ctypes.windll.user32

    def change_token(self):
        return self._user32.GetClipboardSequenceNumber()

    def read_image(self):
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            result = run_powershell(f"""
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$img = [System.Windows.Forms.Clipboard]::GetImage()
if ($img -eq $null) {{ exit 3 }}
$img.Save({_ps_quote(path)}, [System.Drawing.Imaging.ImageFormat]::Png)
""", timeout=20)
            data = Path(path).read_bytes() if result.returncode == 0 else b""
            return data or None
        finally:
            os.unlink(path)


class LinuxClipboard:
    def __init__(self):
        if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
            self._types_cmd = ["wl-paste", "--list-types"]
            self._read_cmd = ["wl-paste", "--no-newline", "--type", "image/png"]
        elif shutil.which("xclip"):
            self._types_cmd = ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"]
            self._read_cmd = ["xclip", "-selection", "clipboard", "-t", "image/png", "-o"]
        else:
            raise RuntimeError("Install wl-clipboard (Wayland) or xclip (X11) for clipboard import.")
        self._last = None

    def change_token(self):
        types = subprocess.run(self._types_cmd, capture_output=True, timeout=5).stdout
        if b"image/png" not in types:
            self._last = None
            return b""
        self._last = subprocess.run(self._read_cmd, capture_output=True, timeout=10).stdout
        return hashlib.sha1(self._last).digest()

    def read_image(self):
        return self._last or None


def make_clipboard():
    if SYSTEM == "Darwin":
        return MacClipboard()
    if SYSTEM == "Windows":
        return WindowsClipboard()
    return LinuxClipboard()


class ClipboardWatcher(threading.Thread):
    def __init__(self, app, interval=1.0):
        super().__init__(daemon=True, name="clipboard-watcher")
        self.app = app
        self.interval = interval
        self.clipboard = make_clipboard()

    def run(self):
        try:
            last = self.clipboard.change_token()
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"Clipboard import disabled: {exc}")
            return
        while True:
            time.sleep(self.interval)
            try:
                token = self.clipboard.change_token()
                if token == last:
                    continue
                last = token
                if not self.app.config.watch_clipboard:
                    continue
                data = self.clipboard.read_image()
                if data and image_info(data):
                    self.app.store.add(data, "clipboard")
            except (OSError, subprocess.SubprocessError, ValueError) as exc:
                log(f"Clipboard read failed: {exc}")


# --------------------------------------------------------------------------- #
# Region capture
# --------------------------------------------------------------------------- #

def _capture_with_tool(path):
    """Run an interactive region capture into path. Returns True when a file was written."""
    if SYSTEM == "Darwin":
        subprocess.run(["screencapture", "-i", str(path)], timeout=300)
    else:
        candidates = []
        if os.environ.get("WAYLAND_DISPLAY") and shutil.which("grim") and shutil.which("slurp"):
            candidates.append(["sh", "-c", f'grim -g "$(slurp)" "{path}"'])
        for tool, args in (
            ("gnome-screenshot", ["-a", "-f", str(path)]),
            ("spectacle", ["-r", "-b", "-n", "-o", str(path)]),
            ("maim", ["-s", str(path)]),
            ("scrot", ["-s", "-o", str(path)]),
        ):
            if shutil.which(tool):
                candidates.append([tool] + args)
        if not candidates:
            raise RuntimeError("No screenshot tool found (install grim+slurp, gnome-screenshot, maim or scrot).")
        subprocess.run(candidates[0], timeout=300)
    return path.exists() and path.stat().st_size > 0


def _capture_windows(app):
    """Open the Windows snipping overlay and wait for the snip to land in the clipboard."""
    clipboard = WindowsClipboard()
    before = clipboard.change_token()
    os.startfile("ms-screenclip:")
    deadline = time.time() + 120
    while time.time() < deadline:
        time.sleep(0.5)
        if clipboard.change_token() != before:
            time.sleep(0.3)
            data = clipboard.read_image()
            if data:
                return app.store.add(data, "capture")
            before = clipboard.change_token()
    return None


def capture_region(app):
    if SYSTEM == "Windows":
        return _capture_windows(app)
    path = DATA_DIR / "capture-tmp.png"
    path.unlink(missing_ok=True)
    try:
        if not _capture_with_tool(path):
            return None
        return app.store.add(path.read_bytes(), "capture")
    finally:
        path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# Global capture shortcut
# --------------------------------------------------------------------------- #

MODIFIER_ALIASES = {
    "cmd": "cmd", "command": "cmd", "win": "cmd", "super": "cmd",
    "ctrl": "ctrl", "control": "ctrl",
    "alt": "alt", "option": "alt", "opt": "alt",
    "shift": "shift",
}
MODIFIER_ORDER = ("ctrl", "alt", "shift", "cmd")


def parse_hotkey(text):
    """'alt+shift+s' -> ({'alt', 'shift'}, 'S'). Raises ValueError."""
    parts = [p.strip().lower() for p in text.split("+") if p.strip()]
    if len(parts) < 2:
        raise ValueError("use at least one modifier and a key, for example alt+shift+s")
    modifiers = set()
    for name in parts[:-1]:
        if name not in MODIFIER_ALIASES:
            raise ValueError(f"unknown modifier {name!r}")
        modifiers.add(MODIFIER_ALIASES[name])
    key = parts[-1].upper()
    if len(key) != 1 or not ("A" <= key <= "Z" or "0" <= key <= "9"):
        raise ValueError("the key must be a letter or a digit")
    return modifiers, key


def hotkey_label(modifiers, key):
    if SYSTEM == "Darwin":
        symbols = {"ctrl": "\u2303", "alt": "\u2325", "shift": "\u21e7", "cmd": "\u2318"}
        return "".join(symbols[m] for m in MODIFIER_ORDER if m in modifiers) + key
    names = {"ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "cmd": "Win"}
    return "+".join([names[m] for m in MODIFIER_ORDER if m in modifiers] + [key])


class MacHotkey:
    """System-wide shortcut via Carbon RegisterEventHotKey (no Accessibility permission needed).

    Carbon delivers hotkey events to the main thread, so run() must be called from it.
    """

    _MODIFIERS = {"cmd": 0x100, "shift": 0x200, "alt": 0x800, "ctrl": 0x1000}
    _KEY_CODES = {
        "A": 0, "S": 1, "D": 2, "F": 3, "H": 4, "G": 5, "Z": 6, "X": 7, "C": 8, "V": 9, "B": 11, "Q": 12,
        "W": 13, "E": 14, "R": 15, "Y": 16, "T": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23,
        "9": 25, "7": 26, "8": 28, "0": 29, "O": 31, "U": 32, "I": 34, "P": 35, "L": 37, "J": 38, "K": 40,
        "N": 45, "M": 46,
    }
    _HOTKEY_EXISTS = -9878

    class _EventTypeSpec(ctypes.Structure):
        _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]

    class _HotKeyID(ctypes.Structure):
        _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]

    _HANDLER = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)

    def __init__(self, modifiers, key, callback):
        path = ctypes.util.find_library("Carbon")
        if not path:
            raise OSError("Carbon framework not found")
        carbon = ctypes.cdll.LoadLibrary(path)
        void_p, ptr = ctypes.c_void_p, ctypes.POINTER
        carbon.GetApplicationEventTarget.restype = void_p
        carbon.GetEventDispatcherTarget.restype = void_p
        carbon.InstallEventHandler.argtypes = [void_p, self._HANDLER, ctypes.c_ulong, ptr(self._EventTypeSpec),
                                               void_p, ptr(void_p)]
        carbon.RegisterEventHotKey.argtypes = [ctypes.c_uint32, ctypes.c_uint32, self._HotKeyID, void_p,
                                               ctypes.c_uint32, ptr(void_p)]
        carbon.ReceiveNextEvent.argtypes = [ctypes.c_ulong, void_p, ctypes.c_double, ctypes.c_ubyte, ptr(void_p)]
        carbon.SendEventToEventTarget.argtypes = [void_p, void_p]
        carbon.ReleaseEvent.argtypes = [void_p]
        self._carbon = carbon

        def on_event(_call_ref, _event, _data):
            try:
                callback()
            except Exception as exc:  # noqa: BLE001 - must not propagate into Carbon
                log(f"Capture shortcut failed: {exc}")
            return 0

        self._handler = self._HANDLER(on_event)  # keep a reference for the lifetime of the app
        target = carbon.GetApplicationEventTarget()
        spec = self._EventTypeSpec(struct.unpack(">I", b"keyb")[0], 5)  # kEventHotKeyPressed
        self._handler_ref = void_p()
        status = carbon.InstallEventHandler(target, self._handler, 1, ctypes.byref(spec), None,
                                            ctypes.byref(self._handler_ref))
        if status:
            raise OSError(f"cannot listen for shortcuts (error {status})")
        mask = sum(self._MODIFIERS[m] for m in modifiers)
        hotkey_id = self._HotKeyID(struct.unpack(">I", b"SNCB")[0], 1)
        self._hotkey_ref = void_p()
        status = carbon.RegisterEventHotKey(self._KEY_CODES[key], mask, hotkey_id, target, 0,
                                            ctypes.byref(self._hotkey_ref))
        if status == self._HOTKEY_EXISTS:
            raise OSError("another app already uses this shortcut")
        if status:
            raise OSError(f"cannot register the shortcut (error {status})")

    def run(self):
        """Dispatch events forever; returns to Python twice a second so Ctrl+C still works."""
        dispatcher = self._carbon.GetEventDispatcherTarget()
        event = ctypes.c_void_p()
        while True:
            if self._carbon.ReceiveNextEvent(0, None, 0.5, 1, ctypes.byref(event)) == 0:
                self._carbon.SendEventToEventTarget(event, dispatcher)
                self._carbon.ReleaseEvent(event)


class WindowsHotkey(threading.Thread):
    """System-wide shortcut via RegisterHotKey; WM_HOTKEY arrives in this thread's message queue."""

    _MODIFIERS = {"alt": 0x1, "ctrl": 0x2, "shift": 0x4, "cmd": 0x8}
    _MOD_NOREPEAT = 0x4000
    _WM_HOTKEY = 0x0312

    def __init__(self, modifiers, key, callback):
        super().__init__(daemon=True, name="hotkey")
        self._mask = sum(self._MODIFIERS[m] for m in modifiers) | self._MOD_NOREPEAT
        self._key = key
        self._callback = callback
        self._ready = threading.Event()
        self._error = None
        self.start()
        self._ready.wait(5)
        if self._error:
            raise self._error

    def run(self):
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        if not user32.RegisterHotKey(None, 1, self._mask, ord(self._key)):
            self._error = OSError("another app already uses this shortcut")
            self._ready.set()
            return
        self._ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == self._WM_HOTKEY:
                self._callback()


def register_hotkey(app):
    """Register the capture shortcut. Returns the MacHotkey whose run() must own the main thread, or None."""
    if not app.config.hotkey:
        return None
    try:
        modifiers, key = parse_hotkey(app.config.hotkey)
        if SYSTEM == "Darwin":
            hotkey = MacHotkey(modifiers, key, app.start_capture)
        elif SYSTEM == "Windows":
            hotkey = None
            WindowsHotkey(modifiers, key, app.start_capture)
        else:
            raise OSError("not supported on Linux; bind `sn_clipboard.py capture` to a shortcut "
                          "in your desktop settings instead")
    except (ValueError, OSError) as exc:
        log(f"Capture shortcut {app.config.hotkey!r} is unavailable: {exc}.")
        return None
    app.hotkey = hotkey_label(modifiers, key)
    return hotkey


# --------------------------------------------------------------------------- #
# App + HTTP server
# --------------------------------------------------------------------------- #

def lan_addresses():
    addresses = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("10.255.255.255", 1))
            addresses.add(sock.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            addresses.add(info[4][0])
    except OSError:
        pass
    return sorted(a for a in addresses if not a.startswith(("127.", "169.254.")))


def computer_name():
    name = socket.gethostname()
    if name.endswith(".local"):
        name = name[:-len(".local")]
    return name or "Computer"


def notify_pair_request(device_name, local_url):
    """Bring the pairing request to the user's attention."""
    if SYSTEM == "Darwin":
        text = f"{device_name} wants to connect. Click Allow in the Supernote Clipboard window."
        script = f'display notification {json.dumps(text)} with title "Supernote Clipboard" sound name "Glass"'
        subprocess.run(["osascript", "-e", script], capture_output=True, timeout=10)
    webbrowser.open(local_url)


class App:
    def __init__(self, config, store):
        self.config = config
        self.store = store
        self.capturing = False
        self.message = ""
        self.hotkey = ""
        self.local_url = f"http://localhost:{config.port}/"
        self._capture_lock = threading.Lock()
        self._pairs = {}
        self._pair_lock = threading.Lock()

    # -- pairing ----------------------------------------------------------- #

    def _expire_pairs(self):
        now = time.time()
        for request_id in [r for r, p in self._pairs.items() if now - p["created"] > PAIR_REQUEST_TTL]:
            del self._pairs[request_id]

    def request_pairing(self, device_name):
        """Start a pairing request. Returns its id, or None when too many are pending."""
        name = (device_name or "Supernote").strip()[:60] or "Supernote"
        with self._pair_lock:
            self._expire_pairs()
            if sum(p["status"] == "pending" for p in self._pairs.values()) >= MAX_PENDING_PAIRS:
                return None
            request_id = secrets.token_urlsafe(18)
            self._pairs[request_id] = {"name": name, "created": time.time(), "status": "pending", "token": None}
        log(f"{name} asks to pair - waiting for approval in the window.")
        threading.Thread(target=notify_pair_request, args=(name, self.local_url), daemon=True).start()
        return request_id

    def pair_status(self, request_id):
        """Pairing result for the device. The token is handed out once, then the request is forgotten."""
        with self._pair_lock:
            self._expire_pairs()
            pair = self._pairs.get(request_id)
            if not pair:
                return None
            if pair["status"] == "pending":
                return {"status": "pending"}
            del self._pairs[request_id]
            if pair["status"] == "approved":
                return {"status": "approved", "token": pair["token"]}
            return {"status": "denied"}

    def answer_pairing(self, request_id, allow):
        with self._pair_lock:
            pair = self._pairs.get(request_id)
            if not pair or pair["status"] != "pending":
                return False
            if allow:
                pair["token"] = self.config.add_device(pair["name"])["token"]
                pair["status"] = "approved"
                log(f"Paired with {pair['name']}.")
            else:
                pair["status"] = "denied"
            return True

    def pending_pairs(self):
        with self._pair_lock:
            self._expire_pairs()
            return [{"id": r, "name": p["name"]} for r, p in self._pairs.items() if p["status"] == "pending"]

    # -- capture ----------------------------------------------------------- #

    def start_capture(self):
        if not self._capture_lock.acquire(blocking=False):
            return False
        self.capturing = True
        self.message = "Select a region on your screen..."

        def worker():
            try:
                shot = capture_region(self)
                self.message = "Screenshot ready. Tap \"Paste screenshot\" on your Supernote." if shot \
                    else "Capture cancelled."
            except Exception as exc:  # noqa: BLE001 - shown to the user in the web UI
                self.message = f"Capture failed: {exc}"
                log(self.message)
            finally:
                self.capturing = False
                self._capture_lock.release()

        threading.Thread(target=worker, daemon=True, name="capture").start()
        return True

    def state(self):
        return {
            "app": APP_NAME,
            "version": VERSION,
            "computerName": computer_name(),
            "port": self.config.port,
            "addresses": lan_addresses(),
            "pendingPairs": self.pending_pairs(),
            "devices": [{k: d[k] for k in ("id", "name", "pairedAt")} for d in self.config.devices],
            "watchClipboard": self.config.watch_clipboard,
            "capturing": self.capturing,
            "hotkey": self.hotkey,
            "message": self.message,
            "revision": self.store.revision,
            "shots": [public_shot(s) for s in self.store.list()],
        }


def public_shot(shot):
    return {k: shot[k] for k in ("id", "ext", "width", "height", "createdAt", "source")}


SHOT_PATH_RE = re.compile(r"^/(api|local)/shots/(\d{8}-\d{6}-[0-9a-f]{4})\.(png|jpg)$")
PAIR_PATH_RE = re.compile(r"^/api/pair/([A-Za-z0-9_-]{16,64})$")


class Handler(BaseHTTPRequestHandler):
    server_version = f"SupernoteClipboard/{VERSION}"
    app = None  # set in serve()
    verbose = False

    seen_clients = set()

    def log_message(self, fmt, *args):
        if self.verbose:
            log(f"{self.client_address[0]} {fmt % args}")

    def _note_client(self):
        host = self.client_address[0]
        if not self._is_local() and host not in self.seen_clients:
            self.seen_clients.add(host)
            log(f"A Supernote reached this computer from {host}.")

    # -- helpers ------------------------------------------------------------ #

    def _is_local(self):
        host = self.client_address[0]
        return host in ("127.0.0.1", "::1") or host.startswith("::ffff:127.")

    def _device(self):
        return self.app.config.device_for_token(self.headers.get("X-SN-Token", ""))

    def _send(self, status, body, content_type, extra_headers=None):
        try:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in (extra_headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, status=200):
        self._send(status, json.dumps(obj).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, status, message):
        self._json({"error": message}, status)

    def _send_shot(self, shot_id, query):
        shot = self.app.store.get(shot_id)
        if not shot:
            return self._error(404, "Screenshot not found.")
        max_width = None
        raw = query.get("maxWidth", [""])[0]
        if raw.isdigit() and 50 <= int(raw) <= 10000:
            max_width = int(raw)
        data = self.app.store.read(shot, max_width)
        ctype = "image/png" if shot["ext"] == "png" else "image/jpeg"
        self._send(200, data, ctype)

    def _read_body(self, limit):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > limit:
            return None
        return self.rfile.read(length)

    # -- routes ------------------------------------------------------------- #

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        url = urllib.parse.urlsplit(self.path)
        query = urllib.parse.parse_qs(url.query)
        path = url.path
        app = self.app

        if path.startswith("/api/"):
            self._note_client()
            device = self._device()
            if path == "/api/hello":
                return self._json({
                    "app": APP_NAME,
                    "version": VERSION,
                    "serverId": app.config.server_id,
                    "name": computer_name(),
                    "paired": device is not None,
                })
            match = PAIR_PATH_RE.match(path)
            if match:
                status = app.pair_status(match.group(1))
                if not status:
                    return self._error(404, "This pairing request expired. Try again.")
                return self._json(status)
            if not device:
                return self._error(401, "This Supernote is not paired with this computer.")
            if path == "/api/latest":
                shot = app.store.latest()
                if not shot:
                    return self._error(404, "No screenshots yet. Take one on your computer first.")
                return self._json({"shot": public_shot(shot)})
            if path == "/api/shots":
                return self._json({"shots": [public_shot(s) for s in app.store.list()]})
            match = SHOT_PATH_RE.match(path)
            if match and match.group(1) == "api":
                return self._send_shot(match.group(2), query)
            return self._error(404, "Not found.")

        if not self._is_local():
            return self._send(403, b"The Supernote Clipboard window is only available on the computer running it.",
                              "text/plain; charset=utf-8")
        if path in ("/", "/index.html"):
            return self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
        if path == "/local/state":
            return self._json(app.state())
        match = SHOT_PATH_RE.match(path)
        if match and match.group(1) == "local":
            return self._send_shot(match.group(2), query)
        return self._error(404, "Not found.")

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        app = self.app

        if path == "/api/pair":
            try:
                payload = json.loads(self._read_body(4096) or b"{}")
            except ValueError:
                payload = {}
            request_id = app.request_pairing(str(payload.get("deviceName", "")))
            if not request_id:
                return self._error(429, "Too many pairing requests. Try again in a minute.")
            return self._json({"requestId": request_id})

        # The custom header forces a CORS preflight, which this server never approves,
        # so other websites open in the browser cannot drive these endpoints.
        if not self._is_local() or self.headers.get("X-SN-Local") != "1":
            return self._error(403, "Forbidden.")

        if path == "/local/upload":
            data = self._read_body(MAX_UPLOAD_BYTES)
            if not data:
                return self._error(400, "Empty or too large upload.")
            try:
                shot = app.store.add(data, "upload")
            except ValueError as exc:
                return self._error(400, str(exc))
            return self._json({"shot": public_shot(shot)})

        body = self._read_body(64 * 1024) or b"{}"
        try:
            payload = json.loads(body)
        except ValueError:
            return self._error(400, "Invalid JSON.")

        if path == "/local/capture":
            started = app.start_capture()
            return self._json({"started": started})
        if path == "/local/promote":
            return self._json({"ok": app.store.promote(str(payload.get("id", "")))})
        if path == "/local/delete":
            return self._json({"ok": app.store.delete(str(payload.get("id", "")))})
        if path == "/local/clear":
            app.store.clear()
            return self._json({"ok": True})
        if path == "/local/settings":
            if "watchClipboard" in payload:
                app.config.watch_clipboard = bool(payload["watchClipboard"])
            app.config.save()
            return self._json({"ok": True})
        if path in ("/local/pair/allow", "/local/pair/deny"):
            ok = app.answer_pairing(str(payload.get("id", "")), path.endswith("allow"))
            return self._json({"ok": ok})
        if path == "/local/devices/remove":
            return self._json({"ok": app.config.remove_device(str(payload.get("id", "")))})
        return self._error(404, "Not found.")


def exit_with_parent():
    """In the macOS app the menu bar process owns this one; never outlive it (or keep the port busy)."""
    parent = os.getppid()

    def watch():
        while os.getppid() == parent:
            time.sleep(2)
        os._exit(0)

    threading.Thread(target=watch, daemon=True, name="parent-watch").start()


def serve(args):
    if args.app:
        args.no_browser = True
        exit_with_parent()
    config = Config(DATA_DIR / "config.json")
    if args.port:
        config.port = args.port
        config.save()
    if args.no_watch:
        config.watch_clipboard = False
        config.save()
    if args.hotkey is not None:
        config.hotkey = "" if args.hotkey.lower() in ("", "off", "none") else args.hotkey
        config.save()
    app = App(config, ShotStore(DATA_DIR / "shots"))
    Handler.app = app
    Handler.verbose = args.verbose

    try:
        server = ThreadingHTTPServer(("0.0.0.0", config.port), Handler)
    except OSError as exc:
        sys.exit(f"Cannot listen on port {config.port}: {exc}\n"
                 f"Is Supernote Clipboard already running? Try --port to pick another port.")
    server.daemon_threads = True

    try:
        ClipboardWatcher(app).start()
    except (RuntimeError, OSError) as exc:
        log(f"Clipboard import unavailable: {exc}")
    mac_hotkey = register_hotkey(app)

    local_url = app.local_url
    addresses = ", ".join(f"{a}:{config.port}" for a in lan_addresses()) or "(no network found)"
    capture = f"press {app.hotkey} anywhere" if app.hotkey else "\"Capture region\" in the window"
    print(f"\n  {APP_NAME} {VERSION}\n")
    print(f"  Window:       {local_url}")
    print(f"  Capture:      {capture}")
    print(f"  Network:      {addresses}")
    print(f"  Paired:       {len(config.devices)} Supernote(s)")
    print(f"  Data folder:  {DATA_DIR}\n  Press Ctrl+C to quit.\n", flush=True)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(local_url)).start()
    try:
        if mac_hotkey:
            threading.Thread(target=server.serve_forever, daemon=True, name="http").start()
            mac_hotkey.run()
        else:
            server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")
    finally:
        server.server_close()


def call_running_instance(path, body, content_type="application/json"):
    config = Config(DATA_DIR / "config.json")
    request = urllib.request.Request(
        f"http://127.0.0.1:{config.port}{path}", data=body, method="POST",
        headers={"X-SN-Local": "1", "Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        sys.exit(f"Error: {json.loads(exc.read() or b'{}').get('error', exc)}")
    except urllib.error.URLError:
        sys.exit("Supernote Clipboard is not running. Start it first with: python3 sn_clipboard.py")


def main():
    parser = argparse.ArgumentParser(description=f"{APP_NAME}: send screenshots to your Supernote.")
    sub = parser.add_subparsers(dest="command")
    serve_parser = sub.add_parser("serve", help="run the app (default)")
    for p in (parser, serve_parser):
        p.add_argument("--port", type=int, help=f"port to listen on (default {DEFAULT_PORT})")
        p.add_argument("--no-browser", action="store_true", help="do not open the window in a browser")
        p.add_argument("--no-watch", action="store_true", help="do not import screenshots from the clipboard")
        p.add_argument("--hotkey", metavar="KEYS",
                       help=f"global capture shortcut, e.g. ctrl+shift+2, or 'off' (default {DEFAULT_HOTKEY or 'off'})")
        p.add_argument("--verbose", action="store_true", help="log every request")
        p.add_argument("--app", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("capture", help="capture a screen region with the running app (bind this to a hotkey)")
    add_parser = sub.add_parser("add", help="send an image file to the running app")
    add_parser.add_argument("file", type=Path)
    args = parser.parse_args()

    if args.command == "capture":
        result = call_running_instance("/local/capture", b"{}")
        print("Select a region on your screen." if result.get("started") else "A capture is already running.")
    elif args.command == "add":
        result = call_running_instance("/local/upload", args.file.read_bytes(), "application/octet-stream")
        print(f"Added {result['shot']['id']}.")
    else:
        serve(args)


# --------------------------------------------------------------------------- #
# Web UI
# --------------------------------------------------------------------------- #

INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Supernote Clipboard</title>
<style>
  :root { --ink:#111; --muted:#666; --line:#ddd; --bg:#f6f6f4; --accent:#111; }
  * { box-sizing:border-box; }
  body { margin:0; font:15px/1.45 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
         color:var(--ink); background:var(--bg); }
  header { display:flex; align-items:center; gap:16px; padding:18px 28px; background:#fff;
           border-bottom:1px solid var(--line); position:sticky; top:0; z-index:1; }
  header h1 { font-size:19px; margin:0; flex:1; }
  .hint { color:var(--muted); font-size:14px; }
  .hint kbd { font:600 14px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; color:var(--ink);
              border:1px solid var(--line); border-radius:5px; padding:1px 6px; background:#fafafa; }
  main { max-width:1100px; margin:0 auto; padding:24px 28px 60px; }
  button { font:inherit; border:1.5px solid var(--ink); background:#fff; color:var(--ink);
           padding:8px 16px; border-radius:8px; cursor:pointer; }
  button.primary { background:var(--ink); color:#fff; }
  button:disabled { opacity:.5; cursor:default; }
  button.small { padding:4px 10px; font-size:13px; }
  .card { background:#fff; border:1px solid var(--line); border-radius:12px; padding:18px 20px; margin-bottom:20px; }
  .setup { display:flex; flex-wrap:wrap; gap:28px; align-items:center; }
  .setup .label { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.06em; }
  .setup .value { font:600 22px/1.2 ui-monospace, Menlo, Consolas, monospace; }
  .row { display:flex; flex-wrap:wrap; gap:12px; align-items:center; }
  .msg { color:var(--muted); min-height:1.4em; margin-top:10px; }
  #drop { border:2px dashed #bbb; border-radius:12px; padding:18px; text-align:center; color:var(--muted);
          margin-bottom:20px; }
  #drop.over { border-color:var(--ink); color:var(--ink); background:#fff; }
  .grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(230px, 1fr)); gap:16px; }
  .shot { background:#fff; border:1px solid var(--line); border-radius:12px; overflow:hidden; display:flex;
          flex-direction:column; }
  .shot.next { border:2px solid var(--ink); }
  .shot .thumb { height:160px; display:flex; align-items:center; justify-content:center; background:#fafafa;
                 border-bottom:1px solid var(--line); }
  .shot img { max-width:100%; max-height:160px; display:block; }
  .shot .meta { padding:10px 12px; font-size:13px; color:var(--muted); flex:1; }
  .shot .meta b { color:var(--ink); }
  .shot .actions { display:flex; gap:8px; padding:0 12px 12px; }
  .badge { display:inline-block; background:var(--ink); color:#fff; font-size:11px; padding:1px 8px;
           border-radius:10px; margin-left:6px; vertical-align:1px; }
  .empty { color:var(--muted); text-align:center; padding:40px 0; }
  label.toggle { display:flex; gap:8px; align-items:center; cursor:pointer; }
  .pair { background:var(--ink); color:#fff; border-radius:12px; padding:18px 20px; margin-bottom:20px;
          display:flex; flex-wrap:wrap; gap:14px; align-items:center; font-size:17px; }
  .pair span { flex:1; min-width:240px; }
  .pair button { border-color:#fff; }
  .pair button.allow { background:#fff; color:var(--ink); font-weight:600; }
  .pair button.deny { background:transparent; color:#fff; }
  .devices { margin-top:12px; display:flex; flex-wrap:wrap; gap:10px; }
  .device { border:1px solid var(--line); border-radius:8px; padding:6px 6px 6px 12px; display:flex; gap:10px;
            align-items:center; }
</style>
</head>
<body>
<header>
  <h1>Supernote Clipboard</h1>
  <span id="hotkey" class="hint"></span>
  <button id="capture" class="primary">Capture region</button>
</header>
<main>
  <div id="pairs"></div>

  <section class="card">
    <div class="setup">
      <div><div class="label">Computer</div><div class="value" id="computer">-</div></div>
      <div style="flex:1; min-width:260px; color:var(--muted)">
        Nothing to type on your Supernote: open a note and tap <b>Paste screenshot</b>. The first time, it finds this
        computer on your Wi-Fi and a request appears here &mdash; click <b>Allow</b> once.
        <span id="manual"></span>
      </div>
    </div>
    <div class="devices" id="devices"></div>
  </section>

  <section class="card">
    <div class="row">
      <label class="toggle"><input type="checkbox" id="watch"> Auto-import screenshots copied to the clipboard</label>
      <span style="flex:1"></span>
      <button id="clear" class="small">Clear all</button>
    </div>
    <div class="msg" id="msg"></div>
  </section>

  <div id="drop">Drop an image here, or press <b>Ctrl/Cmd + V</b> to add one from the clipboard.</div>

  <div class="grid" id="grid"></div>
  <div class="empty" id="empty" hidden>No screenshots yet. Press the capture shortcut, click <b>Capture region</b>, or copy a screenshot.</div>
</main>
<script>
const $ = id => document.getElementById(id);
let lastKey = "";
let lastPairKey = "";

function esc(text) {
  const span = document.createElement("span");
  span.textContent = text;
  return span.innerHTML;
}

function act(path, id) {
  return post(path, { id }).then(refresh).catch(e => alert(e.message));
}

async function post(path, body, type) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "X-SN-Local": "1", "Content-Type": type || "application/json" },
    body: type ? body : JSON.stringify(body || {}),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || res.statusText);
  return data;
}

function fmtTime(ms) {
  const d = new Date(ms);
  return d.toLocaleDateString() + " " + d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function renderPairing(state) {
  const key = JSON.stringify([state.pendingPairs, state.devices, state.addresses]);
  if (key === lastPairKey) return;
  lastPairKey = key;

  $("pairs").innerHTML = state.pendingPairs.map(p => `
    <div class="pair"><span><b>${esc(p.name)}</b> wants to connect to this computer.</span>
      <button class="allow" data-id="${p.id}" data-path="/local/pair/allow">Allow</button>
      <button class="deny" data-id="${p.id}" data-path="/local/pair/deny">Deny</button></div>`).join("");

  $("devices").innerHTML = state.devices.length
    ? '<span style="color:var(--muted)">Paired:</span>' + state.devices.map(d => `
        <span class="device">${esc(d.name)}
          <button class="small" data-id="${d.id}" data-path="/local/devices/remove">Remove</button></span>`).join("")
    : '<span style="color:var(--muted)">No Supernote paired yet.</span>';

  document.querySelectorAll("#pairs button, #devices button").forEach(b =>
    b.onclick = () => act(b.dataset.path, b.dataset.id));

  const addr = state.addresses.map(a => a + ":" + state.port).join(" or ");
  $("manual").textContent = addr ? ` If it is not found automatically, enter ${addr} in the plugin's Settings.` : "";
}

function render(state) {
  $("computer").textContent = state.computerName;
  renderPairing(state);
  $("watch").checked = state.watchClipboard;
  $("capture").disabled = state.capturing;
  $("capture").textContent = state.capturing ? "Capturing..." : "Capture region";
  $("hotkey").innerHTML = state.hotkey ? `Press <kbd>${esc(state.hotkey)}</kbd> anywhere, or` : "";
  $("msg").textContent = state.message || "";

  const key = state.revision + "|" + state.shots.map(s => s.id).join(",");
  if (key === lastKey) return;
  lastKey = key;
  const grid = $("grid");
  grid.innerHTML = "";
  $("empty").hidden = state.shots.length > 0;
  state.shots.forEach((s, i) => {
    const el = document.createElement("div");
    el.className = "shot" + (i === 0 ? " next" : "");
    el.innerHTML = `
      <div class="thumb"><img loading="lazy" src="/local/shots/${s.id}.${s.ext}?maxWidth=480" alt=""></div>
      <div class="meta"><b>${fmtTime(s.createdAt)}</b>${i === 0 ? '<span class="badge">Next paste</span>' : ""}
        <br>${s.width} x ${s.height} px - ${s.source}</div>
      <div class="actions">
        ${i === 0 ? "" : '<button class="small" data-act="promote">Use next</button>'}
        <button class="small" data-act="delete">Delete</button>
      </div>`;
    el.querySelectorAll("button").forEach(b => b.onclick = () => act("/local/" + b.dataset.act, s.id));
    grid.appendChild(el);
  });
}

async function refresh() {
  try {
    const res = await fetch("/local/state");
    render(await res.json());
  } catch (e) {
    $("msg").textContent = "Lost connection to Supernote Clipboard. Is it still running?";
  }
}

async function upload(blob) {
  try { await post("/local/upload", blob, blob.type || "application/octet-stream"); refresh(); }
  catch (e) { alert(e.message); }
}

$("capture").onclick = () => post("/local/capture").then(refresh);
$("watch").onchange = e => post("/local/settings", { watchClipboard: e.target.checked }).then(refresh);
$("clear").onclick = () => confirm("Delete all screenshots?") && post("/local/clear").then(refresh);
document.addEventListener("paste", e => {
  for (const item of e.clipboardData.items) {
    if (item.type.startsWith("image/")) { upload(item.getAsFile()); e.preventDefault(); return; }
  }
});
const drop = $("drop");
["dragenter", "dragover"].forEach(t => document.addEventListener(t, e => {
  e.preventDefault(); drop.classList.add("over");
}));
["dragleave", "drop"].forEach(t => document.addEventListener(t, e => {
  e.preventDefault(); drop.classList.remove("over");
}));
document.addEventListener("drop", e => {
  for (const f of e.dataTransfer.files) if (f.type.startsWith("image/")) upload(f);
});

refresh();
setInterval(refresh, 1500);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
