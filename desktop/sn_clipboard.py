#!/usr/bin/env python3
"""Supernote Clipboard - a clipboard bridge for the "PC Clipboard" Supernote plugin.

Take a screenshot or copy some text on your computer, then tap "Paste from PC" on
your Supernote: images are inserted as grayscale pictures sized for the page, text
as an editable text box.

Items enter the app in three ways:
  * region capture: the global shortcut (Option+Shift+S on macOS, Ctrl+Alt+S on
    Windows), the "Capture region" button, or `sn_clipboard.py capture`
  * any image or text copied to the system clipboard, and image files copied in the
    file manager (auto-import; passwords copied from password managers are skipped)
  * drag & drop or Ctrl/Cmd+V into the web UI

Images may be PNG, JPEG, WebP, HEIC, AVIF, GIF, TIFF, BMP, SVG, PDF, PSD, ICO, JPEG 2000
or TGA; anything other than PNG/JPEG is converted on arrival, since notes only take those.

Storage limits are set in the web UI (or with --keep-days, --max-items, --max-mb):
by default items are deleted after 7 days, only the newest 30 are kept, and there is
no size limit. The window can also keep an item for seconds, minutes, or hours.
Each limit can be turned off. When "delete after paste" is on, a successful paste
on the Supernote removes that item immediately; the time limit still removes the rest.

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
VERSION = "1.3.2"
DEFAULT_PORT = 8765
# Storage limits; 0 means no limit. (default, largest accepted value)
# keep_seconds is the age limit. Older configs stored keep_days and are converted on load.
STORAGE_LIMITS = {
    "keep_seconds": (7 * 86400, 3650 * 86400),
    "max_items": (30, 100000),
    "max_mb": (0, 1000000),
}
MAX_TEXT_CHARS = 20000
MAX_UPLOAD_BYTES = 40 * 1024 * 1024
MAX_FILES_PER_COPY = 20
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
        self.watch_text = True
        self.delete_on_paste = False
        self.limits = {name: default for name, (default, _) in STORAGE_LIMITS.items()}
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
        self.watch_text = bool(data.get("watch_text", True))
        self.delete_on_paste = bool(data.get("delete_on_paste", False))
        limits = {name: data[name] for name in STORAGE_LIMITS if name in data}
        if "keep_seconds" not in data and "keep_days" in data:
            try:
                limits["keep_seconds"] = int(data["keep_days"]) * 86400
            except (TypeError, ValueError):
                pass
        self.set_limits(limits)
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
                "watch_text": self.watch_text,
                "delete_on_paste": self.delete_on_paste,
                **self.limits,
                "hotkey": self.hotkey,
                "devices": self.devices,
            }
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), "utf-8")
            tmp.replace(self.path)

    def set_limits(self, values):
        """Update storage limits from {"keep_seconds"|"max_items"|"max_mb": number}; invalid values are ignored."""
        for name, value in values.items():
            if name not in STORAGE_LIMITS:
                continue
            try:
                self.limits[name] = max(0, min(STORAGE_LIMITS[name][1], int(value)))
            except (TypeError, ValueError):
                pass

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


def format_duration(seconds):
    """'7 days', '1 hour', '2 minutes', '60 seconds'."""
    seconds = int(seconds)
    if seconds % 86400 == 0:
        count, unit = seconds // 86400, "day"
    elif seconds % 3600 == 0:
        count, unit = seconds // 3600, "hour"
    elif seconds % 60 == 0:
        count, unit = seconds // 60, "minute"
    else:
        count, unit = seconds, "second"
    return f"{count} {unit}" if count == 1 else f"{count} {unit}s"


def describe_limits(limits):
    parts = [
        format_duration(limits["keep_seconds"]) if limits["keep_seconds"] else "no time limit",
        f"newest {limits['max_items']} items" if limits["max_items"] else "no item limit",
        f"up to {limits['max_mb']} MB" if limits["max_mb"] else "no size limit",
    ]
    return ", ".join(parts)


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


IMAGE_FORMAT_NAMES = {
    "png": "PNG", "jpg": "JPEG", "webp": "WebP", "heic": "HEIC", "avif": "AVIF", "gif": "GIF",
    "tiff": "TIFF", "bmp": "BMP", "svg": "SVG", "pdf": "PDF", "psd": "Photoshop", "ico": "ICO",
    "jp2": "JPEG 2000", "tga": "TGA",
}
IMAGE_EXTENSIONS = {
    ".png": "png", ".jpg": "jpg", ".jpeg": "jpg", ".jpe": "jpg", ".webp": "webp", ".heic": "heic",
    ".heif": "heic", ".hif": "heic", ".avif": "avif", ".gif": "gif", ".tif": "tiff", ".tiff": "tiff", ".bmp": "bmp",
    ".dib": "bmp", ".svg": "svg", ".pdf": "pdf", ".psd": "psd", ".ico": "ico", ".jp2": "jp2", ".j2k": "jp2",
    ".tga": "tga",
}
# Image files copied in a file manager are imported; PDFs only when copied as image data.
IMAGE_FILE_EXTENSIONS = set(IMAGE_EXTENSIONS) - {".pdf"}
# Photos stay JPEG after conversion; PNG would make them several times larger.
PHOTO_FORMATS = {"heic", "avif", "jp2"}
UNSUPPORTED_IMAGE = ("This file is not an image Supernote Clipboard can read. Supported: "
                     + ", ".join(IMAGE_FORMAT_NAMES.values()) + ".")


def detect_image_format(data, name=""):
    """Image format from the file contents (or the file name for formats without a signature)."""
    head = data[:64]
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"\xff\xd8"):
        return "jpg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"BM"):
        return "bmp"
    if head.startswith((b"II*\x00", b"MM\x00*")):
        return "tiff"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"avif", b"avis"):
            return "avif"
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
            return "heic"
    if head.startswith((b"\x00\x00\x00\x0cjP  ", b"\xff\x4f\xff\x51")):
        return "jp2"
    if head.startswith(b"8BPS"):
        return "psd"
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"\x00\x00\x01\x00"):
        return "ico"
    if b"<svg" in data[:4096].lower():
        return "svg"
    return IMAGE_EXTENSIONS.get(Path(name).suffix.lower()) if name else None


def _convert_format(src, dst):
    """Re-encode src as dst (PNG or JPEG, by suffix) with the platform's image tools."""
    jpeg = dst.suffix == ".jpg"
    if SYSTEM == "Darwin":
        # sips reads WebP, HEIC, AVIF, SVG, PDF (first page), PSD, TGA, ... and uses the first frame of a GIF.
        cmd = ["sips", "-s", "format", "jpeg" if jpeg else "png"]
        if jpeg:
            cmd += ["-s", "formatOptions", "90"]
        subprocess.run(cmd + [str(src), "--out", str(dst)], capture_output=True, timeout=60)
    elif SYSTEM == "Windows":
        # WIC reads what Windows has codecs for (WebP, HEIC and AVIF need the free Store extensions).
        encoder = "JpegBitmapEncoder" if jpeg else "PngBitmapEncoder"
        run_powershell(f"""
Add-Type -AssemblyName PresentationCore
$in = [System.IO.File]::OpenRead({_ps_quote(src)})
try {{
  $decoder = [System.Windows.Media.Imaging.BitmapDecoder]::Create($in,
    [System.Windows.Media.Imaging.BitmapCreateOptions]::None, [System.Windows.Media.Imaging.BitmapCacheOption]::OnLoad)
  $encoder = New-Object System.Windows.Media.Imaging.{encoder}
  $encoder.Frames.Add([System.Windows.Media.Imaging.BitmapFrame]::Create($decoder.Frames[0]))
  $out = [System.IO.File]::Create({_ps_quote(dst)})
  try {{ $encoder.Save($out) }} finally {{ $out.Close() }}
}} finally {{ $in.Close() }}
""", timeout=60)
    else:
        tool = shutil.which("magick") or shutil.which("convert")
        if tool:
            subprocess.run([tool, f"{src}[0]", str(dst)], capture_output=True, timeout=60)


def to_supported_image(data, name=""):
    """Return data as PNG or JPEG (the formats a Supernote note accepts), converting other formats."""
    fmt = detect_image_format(data, name)
    if fmt in ("png", "jpg"):
        return data
    if not fmt:
        raise ValueError(UNSUPPORTED_IMAGE)
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / f"in.{fmt}"
        dst = Path(tmp) / ("out.jpg" if fmt in PHOTO_FORMATS else "out.png")
        src.write_bytes(data)
        try:
            _convert_format(src, dst)
        except (OSError, subprocess.SubprocessError):
            pass
        converted = dst.read_bytes() if dst.exists() else b""
    if not image_info(converted):
        raise ValueError(f"This {IMAGE_FORMAT_NAMES[fmt]} image could not be converted on this computer.")
    return converted


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


MAC_GRAY_PROFILE = "/System/Library/ColorSync/Profiles/Generic Gray Gamma 2.2 Profile.icc"


def _sips(src, dst, args):
    subprocess.run(["sips", *args, str(src), "--out", str(dst)], capture_output=True, timeout=30)
    return dst.exists() and dst.stat().st_size > 0


def _windows_convert(src, dst, max_width, gray):
    """Resize and, when gray is set, convert to grayscale."""
    fmt = "Png" if dst.suffix == ".png" else "Jpeg"
    gray_matrix = """
$cm.Matrix00 = 0.299; $cm.Matrix01 = 0.299; $cm.Matrix02 = 0.299
$cm.Matrix10 = 0.587; $cm.Matrix11 = 0.587; $cm.Matrix12 = 0.587
$cm.Matrix20 = 0.114; $cm.Matrix21 = 0.114; $cm.Matrix22 = 0.114
""" if gray else ""
    run_powershell(f"""
Add-Type -AssemblyName System.Drawing
$src = [System.Drawing.Image]::FromFile({_ps_quote(src)})
$w = {int(max_width) if max_width else "$src.Width"}
$h = [int][Math]::Round($src.Height * $w / $src.Width)
$bmp = New-Object System.Drawing.Bitmap $w, $h
$g = [System.Drawing.Graphics]::FromImage($bmp)
$g.InterpolationMode = [System.Drawing.Drawing2D.InterpolationMode]::HighQualityBicubic
$cm = New-Object System.Drawing.Imaging.ColorMatrix
{gray_matrix}
$attr = New-Object System.Drawing.Imaging.ImageAttributes
$attr.SetColorMatrix($cm)
$rect = New-Object System.Drawing.Rectangle 0, 0, $w, $h
$g.DrawImage($src, $rect, 0, 0, $src.Width, $src.Height, [System.Drawing.GraphicsUnit]::Pixel, $attr)
$g.Dispose(); $src.Dispose()
$bmp.Save({_ps_quote(dst)}, [System.Drawing.Imaging.ImageFormat]::{fmt})
$bmp.Dispose()
""")
    return dst.exists() and dst.stat().st_size > 0


def convert_image(src, dst, max_width=None, gray=False):
    """Write src to dst, scaled down to max_width pixels wide and/or converted to grayscale.

    Returns True when dst was written. The picture is resized first, then converted to grayscale.
    """
    scaled = None
    try:
        if SYSTEM == "Darwin":
            source = src
            if max_width:
                scaled = dst.with_name(dst.stem + "-scaled" + dst.suffix)
                if not _sips(src, scaled, ["--resampleWidth", str(int(max_width))]):
                    return False
                source = scaled
            if gray:
                if not _sips(source, dst, ["--matchTo", MAC_GRAY_PROFILE]):
                    return False
            elif scaled is not None:
                scaled.replace(dst)
                scaled = None
            else:
                _sips(src, dst, [])
        elif SYSTEM == "Windows":
            source = src
            if max_width:
                scaled = dst.with_name(dst.stem + "-scaled" + dst.suffix)
                if not _windows_convert(src, scaled, max_width, gray=False):
                    return False
                source = scaled
            if gray:
                if not _windows_convert(source, dst, None, gray=True):
                    return False
            elif scaled is not None:
                scaled.replace(dst)
                scaled = None
            else:
                _windows_convert(src, dst, None, gray=False)
        else:
            tool = shutil.which("magick") or shutil.which("convert")
            if not tool:
                return False
            source = src
            if max_width:
                scaled = dst.with_name(dst.stem + "-scaled" + dst.suffix)
                subprocess.run(
                    [tool, str(src), "-resize", f"{int(max_width)}x", str(scaled)],
                    capture_output=True, timeout=30,
                )
                if not (scaled.exists() and scaled.stat().st_size > 0):
                    return False
                source = scaled
            if gray:
                subprocess.run([tool, str(source), "-colorspace", "Gray", str(dst)], capture_output=True, timeout=30)
            elif scaled is not None:
                scaled.replace(dst)
                scaled = None
            else:
                subprocess.run([tool, str(src), str(dst)], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    finally:
        if scaled is not None and scaled.exists():
            scaled.unlink()
    return dst.exists() and dst.stat().st_size > 0


# --------------------------------------------------------------------------- #
# Clipboard item store
# --------------------------------------------------------------------------- #

def normalize_text(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    return text[:MAX_TEXT_CHARS] if text.strip() else ""


class ClipStore:
    """Images and text waiting to be pasted on a Supernote, newest first."""

    def __init__(self, directory, limits):
        self.dir = directory
        self.cache_dir = directory / "cache"
        self.index_path = directory / "index.json"
        self.limits = limits  # shared with Config, see STORAGE_LIMITS
        self.lock = threading.RLock()
        self.shots = []
        self.usage = 0
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
            entry.setdefault("kind", "image")
            if SHOT_ID_RE.match(str(entry.get("id", ""))) and self._file(entry).exists():
                self.shots.append(entry)

    def _save(self):
        self.revision += 1
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.shots, indent=1), "utf-8")
        tmp.replace(self.index_path)

    def _file(self, shot):
        return self.dir / f"{shot['id']}.{shot['ext']}"

    def _files(self, shot):
        return [self._file(shot), *self.cache_dir.glob(f"{shot['id']}_*")]

    def _remove_files(self, shot):
        for path in self._files(shot):
            path.unlink(missing_ok=True)

    def _bytes(self, shot):
        """Disk space of an item, including the scaled copies made for the Supernote."""
        total = 0
        for path in self._files(shot):
            try:
                total += path.stat().st_size
            except OSError:
                pass
        return total

    def _insert(self, item, data, digest):
        """Store a new item, or move an identical one back to the top. Returns (item, is_new)."""
        with self.lock:
            for existing in self.shots:
                if existing["sha1"] == digest:
                    existing["createdAt"] = item["createdAt"]
                    self.promote(existing["id"])
                    return existing, False
            self._file(item).write_bytes(data)
            self.shots.insert(0, item)
            self._save()
            self.prune()
        return item, True

    @staticmethod
    def _new_item(kind, ext, source, digest, **extra):
        return {
            "id": time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2),
            "kind": kind,
            "ext": ext,
            "createdAt": int(time.time() * 1000),
            "source": source,
            "sha1": digest,
            **extra,
        }

    def add(self, data, source, name=""):
        """Add an image in any format detect_image_format knows; name helps for formats without a signature."""
        digest = hashlib.sha1(data).hexdigest()
        data = to_supported_image(data, name)
        info = image_info(data)
        if not info:
            raise ValueError(UNSUPPORTED_IMAGE)
        ext, width, height = info
        shot, is_new = self._insert(
            self._new_item("image", ext, source, digest, width=width, height=height), data, digest)
        if is_new:
            log(f"New image {shot['id']} ({width}x{height}, from {source})")
        return shot

    def add_text(self, text, source):
        text = normalize_text(text)
        if not text:
            raise ValueError("The text is empty.")
        data = text.encode("utf-8")
        digest = hashlib.sha1(b"text:" + data).hexdigest()
        item, is_new = self._insert(
            self._new_item("text", "txt", source, digest, width=0, height=0, chars=len(text),
                           preview=text[:280]), data, digest)
        if is_new:
            log(f"New text {item['id']} ({len(text)} characters, from {source})")
        return item

    def prune(self):
        """Apply the storage limits, oldest items first. The size limit never deletes the newest item."""
        keep_seconds, max_items, max_mb = (self.limits[k] for k in ("keep_seconds", "max_items", "max_mb"))
        removed = {"age": 0, "count": 0, "size": 0}
        with self.lock:
            kept = list(self.shots)
            if keep_seconds:
                cutoff = (time.time() - keep_seconds) * 1000
                fresh = [s for s in kept if s["createdAt"] >= cutoff]
                removed["age"] = len(kept) - len(fresh)
                kept = fresh
            if max_items and len(kept) > max_items:
                removed["count"] = len(kept) - max_items
                kept = kept[:max_items]
            sizes = [self._bytes(s) for s in kept]
            if max_mb:
                while len(kept) > 1 and sum(sizes) > max_mb * 1024 * 1024:
                    kept.pop()
                    sizes.pop()
                    removed["size"] += 1
            if len(kept) != len(self.shots):
                kept_ids = {s["id"] for s in kept}
                for shot in self.shots:
                    if shot["id"] not in kept_ids:
                        self._remove_files(shot)
                self.shots = kept
                self._save()
            self.usage = sum(sizes)
        reasons = {
            "age": f"older than {format_duration(keep_seconds)}",
            "count": f"beyond the newest {max_items}",
            "size": f"over {max_mb} MB",
        }
        for key, count in removed.items():
            if count:
                log(f"Deleted {count} item(s) {reasons[key]}.")
        return sum(removed.values())

    def list(self, kinds=("image",)):
        with self.lock:
            return [dict(s) for s in self.shots if s["kind"] in kinds]

    def latest(self, kinds=("image",)):
        items = self.list(kinds)
        return items[0] if items else None

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
                    self.usage = sum(self._bytes(s) for s in self.shots)
                    return True
        return False

    def clear(self):
        with self.lock:
            for shot in self.shots:
                self._remove_files(shot)
            self.shots = []
            self.usage = 0
            self._save()

    def read(self, shot, max_width=None, gray=False):
        """Return image bytes, scaled down to max_width when the original is wider, optionally grayscale."""
        src = self._file(shot)
        if max_width and shot["width"] <= max_width:
            max_width = None
        if not max_width and not gray:
            return src.read_bytes()
        suffix = (f"_w{max_width}" if max_width else "") + ("_g100" if gray else "")
        cached = self.cache_dir / f"{shot['id']}{suffix}.{shot['ext']}"
        if cached.exists() or convert_image(src, cached, max_width, gray):
            return cached.read_bytes()
        return src.read_bytes()

    def read_text(self, item):
        return self._file(item).read_text("utf-8")


# --------------------------------------------------------------------------- #
# Clipboard access
# --------------------------------------------------------------------------- #

URL_RE = re.compile(r"^(https?|file)://\S+$")


def prefer_image(has_image, text):
    """Copying a picture in a browser also puts its address on the clipboard; anything else with text is text."""
    if not text or not text.strip():
        return has_image
    return has_image and bool(URL_RE.match(text.strip()))


def is_image_file(path):
    """By extension, or by content for unusual ones (cameras use .HIF, .JFIF, ...)."""
    if path.suffix.lower() in IMAGE_FILE_EXTENSIONS:
        return True
    try:
        with open(path, "rb") as f:
            fmt = detect_image_format(f.read(4096))
    except OSError:
        return False
    return fmt not in (None, "pdf")


def image_files(paths):
    return [p for p in paths if p.is_file() and is_image_file(p)]


class MacClipboard:
    """Reads the general pasteboard.

    read() returns ("image", bytes in any image format), ("files", [image paths]), ("text", str) or None.
    """

    _EXPORT_SCRIPT = (
        'on run argv\n'
        '  set png to the clipboard as \u00abclass PNGf\u00bb\n'
        '  set fh to open for access (POSIX file (item 1 of argv)) with write permission\n'
        '  set eof fh to 0\n'
        '  write png to fh\n'
        '  close access fh\n'
        'end run\n'
    )
    # Markers password managers and clipboard tools put on secrets (see nspasteboard.org).
    _CONCEALED_TYPES = {
        "org.nspasteboard.ConcealedType", "org.nspasteboard.TransientType",
        "org.nspasteboard.AutoGeneratedType", "com.agilebits.onepassword",
    }
    # In order of preference: apps usually add a TIFF next to the original format, and PDF for vector art.
    _IMAGE_TYPES = (
        "public.png", "public.jpeg", "org.webmproject.webp", "public.heic", "public.heif", "public.avif",
        "com.compuserve.gif", "public.tiff", "com.microsoft.bmp", "public.jpeg-2000",
        "com.adobe.photoshop-image", "com.microsoft.ico", "public.svg-image", "com.adobe.pdf",
    )
    _TEXT_TYPE = "public.utf8-plain-text"
    _FILE_TYPE = "public.file-url"

    def __init__(self):
        self._objc = None
        try:
            objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
            ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AppKit.framework/AppKit")
            void_p = ctypes.c_void_p
            objc.objc_getClass.restype = void_p
            objc.objc_getClass.argtypes = [ctypes.c_char_p]
            objc.sel_registerName.restype = void_p
            objc.sel_registerName.argtypes = [ctypes.c_char_p]
            objc.objc_autoreleasePoolPush.restype = void_p
            objc.objc_autoreleasePoolPop.argtypes = [void_p]

            def message(restype, *argtypes):
                return ctypes.CFUNCTYPE(restype, void_p, void_p, *argtypes)(("objc_msgSend", objc))

            self._send = message(void_p)
            self._send_long = message(ctypes.c_long)
            self._send_index = message(void_p, ctypes.c_ulong)
            self._send_obj = message(void_p, void_p)
            self._send_cstr = message(void_p, ctypes.c_char_p)
            self._send_utf8 = message(ctypes.c_char_p)
            self._sel = lambda name: objc.sel_registerName(name.encode())
            self._nsstring = objc.objc_getClass(b"NSString")
            self._board = self._send(objc.objc_getClass(b"NSPasteboard"), self._sel("generalPasteboard"))
            if self._board:
                self._objc = objc
        except (OSError, AttributeError, TypeError):
            self._objc = None

    def change_token(self):
        if self._objc:
            return self._send_long(self._board, self._sel("changeCount"))
        out = subprocess.run(["osascript", "-e", "clipboard info"], capture_output=True, timeout=10)
        return out.stdout

    def _utf8(self, nsstring):
        raw = self._send_utf8(nsstring, self._sel("UTF8String")) if nsstring else None
        return raw.decode("utf-8", "replace") if raw is not None else None

    def _types(self):
        array = self._send(self._board, self._sel("types"))
        count = self._send_long(array, self._sel("count")) if array else 0
        return {self._utf8(self._send_index(array, self._sel("objectAtIndex:"), i)) for i in range(count)}

    def _key(self, uti):
        return self._send_cstr(self._nsstring, self._sel("stringWithUTF8String:"), uti.encode())

    def _string(self, uti):
        return self._utf8(self._send_obj(self._board, self._sel("stringForType:"), self._key(uti)))

    def _data(self, uti):
        nsdata = self._send_obj(self._board, self._sel("dataForType:"), self._key(uti))
        length = self._send_long(nsdata, self._sel("length")) if nsdata else 0
        pointer = self._send(nsdata, self._sel("bytes")) if length else None
        return ctypes.string_at(pointer, length) if pointer else None

    def _file_paths(self):
        """Every file copied in Finder (one pasteboard item each)."""
        items = self._send(self._board, self._sel("pasteboardItems"))
        count = self._send_long(items, self._sel("count")) if items else 0
        key = self._key(self._FILE_TYPE)
        nsurl = self._objc.objc_getClass(b"NSURL")
        paths = []
        for i in range(count):
            item = self._send_index(items, self._sel("objectAtIndex:"), i)
            url = self._send_obj(item, self._sel("stringForType:"), key)
            # Finder copies file reference URLs (file:///.file/id=...); filePathURL turns them into paths.
            url = self._send_obj(nsurl, self._sel("URLWithString:"), url) if url else None
            url = self._send(url, self._sel("filePathURL")) if url else None
            path = self._utf8(self._send(url, self._sel("path"))) if url else None
            if path:
                paths.append(Path(path))
        return paths

    def read(self, want_image=True, want_text=True):
        if not self._objc:
            # Without the pasteboard types, secrets cannot be told apart from text: images only.
            data = self.read_image() if want_image else None
            return ("image", data) if data else None
        pool = self._objc.objc_autoreleasePoolPush()
        try:
            types = self._types()
            if types & self._CONCEALED_TYPES:
                return None
            text = self._string(self._TEXT_TYPE) if self._TEXT_TYPE in types else None
            # Files copied in Finder also put their names and icons on the clipboard, which are not wanted.
            files = self._file_paths() if self._FILE_TYPE in types else []
            image_type = next((t for t in self._IMAGE_TYPES if t in types), None)
            data = None
            if not files and want_image and prefer_image(image_type is not None, text):
                data = self._data(image_type)
        finally:
            self._objc.objc_autoreleasePoolPop(pool)

        if files:
            images = image_files(files)
            return ("files", images) if want_image and images else None
        if prefer_image(image_type is not None, text):
            data = data or (self.read_image() if want_image else None)
            return ("image", data) if data else None
        return ("text", text) if text and want_text else None

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
    CF_BITMAP = 2
    CF_DIB = 8
    CF_UNICODETEXT = 13
    CF_HDROP = 15
    # Formats password managers add so clipboard tools leave the content alone.
    _PRIVATE_FORMATS = ("ExcludeClipboardContentFromMonitorProcessing", "Clipboard Viewer Ignore")

    def __init__(self):
        void_p = ctypes.c_void_p
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        user32.OpenClipboard.argtypes = [void_p]
        user32.GetClipboardData.argtypes = [ctypes.c_uint]
        user32.GetClipboardData.restype = void_p
        user32.IsClipboardFormatAvailable.argtypes = [ctypes.c_uint]
        user32.RegisterClipboardFormatW.argtypes = [ctypes.c_wchar_p]
        user32.RegisterClipboardFormatW.restype = ctypes.c_uint
        kernel32.GlobalLock.argtypes = [void_p]
        kernel32.GlobalLock.restype = void_p
        kernel32.GlobalUnlock.argtypes = [void_p]
        shell32 = ctypes.windll.shell32
        shell32.DragQueryFileW.argtypes = [void_p, ctypes.c_uint, ctypes.c_wchar_p, ctypes.c_uint]
        shell32.DragQueryFileW.restype = ctypes.c_uint
        self._user32, self._kernel32, self._shell32 = user32, kernel32, shell32
        self._private = [user32.RegisterClipboardFormatW(name) for name in self._PRIVATE_FORMATS]

    def change_token(self):
        return self._user32.GetClipboardSequenceNumber()

    def _has(self, fmt):
        return bool(fmt and self._user32.IsClipboardFormatAvailable(fmt))

    def _open(self):
        for _ in range(10):  # another program may hold the clipboard for a moment
            if self._user32.OpenClipboard(None):
                return True
            time.sleep(0.05)
        return False

    def _read_text(self):
        if not self._open():
            return None
        try:
            handle = self._user32.GetClipboardData(self.CF_UNICODETEXT)
            pointer = self._kernel32.GlobalLock(handle) if handle else None
            if not pointer:
                return None
            try:
                return ctypes.wstring_at(pointer)
            finally:
                self._kernel32.GlobalUnlock(handle)
        finally:
            self._user32.CloseClipboard()

    def _read_files(self):
        """Files copied in Explorer."""
        if not self._open():
            return []
        try:
            handle = self._user32.GetClipboardData(self.CF_HDROP)
            if not handle:
                return []
            paths = []
            for i in range(self._shell32.DragQueryFileW(handle, 0xFFFFFFFF, None, 0)):
                size = self._shell32.DragQueryFileW(handle, i, None, 0) + 1
                buffer = ctypes.create_unicode_buffer(size)
                self._shell32.DragQueryFileW(handle, i, buffer, size)
                paths.append(Path(buffer.value))
            return paths
        finally:
            self._user32.CloseClipboard()

    def read(self, want_image=True, want_text=True):
        if any(self._has(fmt) for fmt in self._private):
            return None
        if self._has(self.CF_HDROP):
            images = image_files(self._read_files())
            return ("files", images) if want_image and images else None
        text = self._read_text() if self._has(self.CF_UNICODETEXT) else None
        if prefer_image(self._has(self.CF_DIB) or self._has(self.CF_BITMAP), text):
            data = self.read_image() if want_image else None
            return ("image", data) if data else None
        return ("text", text) if text and want_text else None

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
    _TEXT_TYPES = ("text/plain;charset=utf-8", "UTF8_STRING", "text/plain")
    _IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp", "image/gif", "image/avif", "image/heic",
                    "image/tiff", "image/bmp", "image/svg+xml")
    _SKIP_TYPES = {"x-kde-passwordManagerHint", "x-special/gnome-copied-files"}

    def __init__(self):
        if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-paste"):
            self._types_cmd = ["wl-paste", "--list-types"]
            self._read_cmd = lambda mime: ["wl-paste", "--no-newline", "--type", mime]
        elif shutil.which("xclip"):
            self._types_cmd = ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"]
            self._read_cmd = lambda mime: ["xclip", "-selection", "clipboard", "-t", mime, "-o"]
        else:
            raise RuntimeError("Install wl-clipboard (Wayland) or xclip (X11) for clipboard import.")
        self._last = None

    def _fetch(self, mime):
        return subprocess.run(self._read_cmd(mime), capture_output=True, timeout=10).stdout

    def _snapshot(self):
        types = set(subprocess.run(self._types_cmd, capture_output=True, timeout=5).stdout
                    .decode("utf-8", "replace").split())
        if types & self._SKIP_TYPES:
            return None
        mime = next((t for t in self._TEXT_TYPES if t in types), None)
        text = self._fetch(mime).decode("utf-8", "replace") if mime else None
        image_type = next((t for t in self._IMAGE_TYPES if t in types), None)
        if prefer_image(image_type is not None, text):
            data = self._fetch(image_type)
            return ("image", data) if data else None
        return ("text", text) if text else None

    def change_token(self):
        self._last = self._snapshot()
        if not self._last:
            return b""
        kind, value = self._last
        return kind.encode() + hashlib.sha1(value if kind == "image" else value.encode()).digest()

    def read(self, want_image=True, want_text=True):
        if not self._last:
            return None
        return self._last if (want_image if self._last[0] == "image" else want_text) else None


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

    def _add_files(self, paths):
        for path in paths[:MAX_FILES_PER_COPY]:
            try:
                if path.is_file() and path.stat().st_size <= MAX_UPLOAD_BYTES:
                    self.app.store.add(path.read_bytes(), "file", path.name)
            except (OSError, ValueError) as exc:
                log(f"Skipped {path.name}: {exc}")

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
                config = self.app.config
                if not (config.watch_clipboard or config.watch_text):
                    continue
                clip = self.clipboard.read(config.watch_clipboard, config.watch_text)
                if not clip:
                    continue
                kind, value = clip
                if kind == "image":
                    self.app.store.add(value, "clipboard")
                elif kind == "files":
                    self._add_files(value)
                elif kind == "text" and normalize_text(value):
                    self.app.store.add_text(value, "clipboard")
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
                self.message = "Screenshot ready. Tap \"Paste from PC\" on your Supernote." if shot \
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
            "watchText": self.config.watch_text,
            "deleteOnPaste": self.config.delete_on_paste,
            "limits": dict(self.config.limits),
            "usageBytes": self.store.usage,
            "capturing": self.capturing,
            "hotkey": self.hotkey,
            "message": self.message,
            "revision": self.store.revision,
            "shots": [public_shot(s) for s in self.store.list(ALL_KINDS)],
        }


ALL_KINDS = ("image", "text")


def public_shot(shot):
    keys = ("id", "kind", "ext", "width", "height", "createdAt", "source")
    if shot["kind"] == "text":
        keys += ("chars", "preview")
    return {k: shot[k] for k in keys}


def requested_kinds(query):
    """Plugins before 1.3 only understand images, so text is only listed when asked for."""
    kinds = tuple(k for k in query.get("kinds", ["image"])[0].split(",") if k in ALL_KINDS)
    return kinds or ("image",)


SHOT_PATH_RE = re.compile(r"^/(api|local)/shots/(\d{8}-\d{6}-[0-9a-f]{4})\.(png|jpg)$")
TEXT_PATH_RE = re.compile(r"^/api/text/(\d{8}-\d{6}-[0-9a-f]{4})$")
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

    def _send_shot(self, shot_id, query, gray):
        shot = self.app.store.get(shot_id)
        if not shot or shot["kind"] != "image":
            return self._error(404, "Screenshot not found.")
        max_width = None
        raw = query.get("maxWidth", [""])[0]
        if raw.isdigit() and 50 <= int(raw) <= 10000:
            max_width = int(raw)
        data = self.app.store.read(shot, max_width, gray)
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
                shot = app.store.latest(requested_kinds(query))
                if not shot:
                    return self._error(404, "Nothing copied yet. Take a screenshot on your computer first.")
                return self._json({"shot": public_shot(shot)})
            if path == "/api/shots":
                return self._json({"shots": [public_shot(s) for s in app.store.list(requested_kinds(query))]})
            match = SHOT_PATH_RE.match(path)
            if match and match.group(1) == "api":
                # The Supernote screen is grayscale; converting here keeps the files small.
                return self._send_shot(match.group(2), query, gray=True)
            match = TEXT_PATH_RE.match(path)
            if match:
                item = app.store.get(match.group(1))
                if not item or item["kind"] != "text":
                    return self._error(404, "This text was deleted on your computer.")
                return self._json({"text": app.store.read_text(item)})
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
            return self._send_shot(match.group(2), query, gray=False)
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

        if path == "/api/pasted":
            self._note_client()
            if not self._device():
                return self._error(401, "This Supernote is not paired with this computer.")
            try:
                payload = json.loads(self._read_body(4096) or b"{}")
            except ValueError:
                return self._error(400, "Invalid JSON.")
            deleted = app.config.delete_on_paste and app.store.delete(str(payload.get("id", "")))
            return self._json({"ok": True, "deleted": bool(deleted)})

        # The custom header forces a CORS preflight, which this server never approves,
        # so other websites open in the browser cannot drive these endpoints.
        if not self._is_local() or self.headers.get("X-SN-Local") != "1":
            return self._error(403, "Forbidden.")

        if path == "/local/upload":
            data = self._read_body(MAX_UPLOAD_BYTES)
            if not data:
                return self._error(400, "Empty or too large upload.")
            name = urllib.parse.unquote(self.headers.get("X-SN-Filename", ""))[:200]
            try:
                shot = app.store.add(data, "upload", name)
            except ValueError as exc:
                return self._error(400, str(exc))
            return self._json({"shot": public_shot(shot)})
        if path == "/local/text":
            data = self._read_body(MAX_TEXT_CHARS * 4)
            try:
                shot = app.store.add_text(data.decode("utf-8") if data else "", "upload")
            except (UnicodeDecodeError, ValueError) as exc:
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
            if "watchText" in payload:
                app.config.watch_text = bool(payload["watchText"])
            if "deleteOnPaste" in payload:
                app.config.delete_on_paste = bool(payload["deleteOnPaste"])
            if isinstance(payload.get("limits"), dict):
                app.config.set_limits(payload["limits"])
                app.store.prune()
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


def start_pruning(store):
    def loop():
        while True:
            keep = store.limits.get("keep_seconds") or 0
            # A one-minute limit cannot wait for the old ten-minute pass.
            time.sleep(5 if 0 < keep < 3600 else 60)
            store.prune()

    threading.Thread(target=loop, daemon=True, name="prune").start()


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
    limit_args = {name: getattr(args, name) for name in ("max_items", "max_mb") if getattr(args, name, None) is not None}
    if getattr(args, "keep_days", None) is not None:
        limit_args["keep_seconds"] = max(0, args.keep_days) * 86400
    if limit_args:
        config.set_limits(limit_args)
        config.save()
    store = ClipStore(DATA_DIR / "shots", config.limits)
    store.prune()
    app = App(config, store)
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
    start_pruning(store)
    mac_hotkey = register_hotkey(app)

    local_url = app.local_url
    addresses = ", ".join(f"{a}:{config.port}" for a in lan_addresses()) or "(no network found)"
    capture = f"press {app.hotkey} anywhere" if app.hotkey else "\"Capture region\" in the window"
    print(f"\n  {APP_NAME} {VERSION}\n")
    print(f"  Window:       {local_url}")
    print(f"  Capture:      {capture}")
    print(f"  Network:      {addresses}")
    print(f"  Paired:       {len(config.devices)} Supernote(s)")
    keeps = describe_limits(config.limits)
    if config.delete_on_paste:
        keeps += ", delete on paste"
    print(f"  Keeps:        {keeps}")
    print(f"  Data folder:  {DATA_DIR}")
    if SYSTEM == "Windows":
        print("  Firewall:     If Windows asks, allow this app on private networks.")
        print("                That allow covers every port, including one you choose later.")
    quit_hint = "Close this window to quit." if getattr(sys, "frozen", False) else "Press Ctrl+C to quit."
    print(f"  {quit_hint}\n", flush=True)
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


def call_running_instance(path, body, content_type="application/json", headers=None):
    config = Config(DATA_DIR / "config.json")
    request = urllib.request.Request(
        f"http://127.0.0.1:{config.port}{path}", data=body, method="POST",
        headers={"X-SN-Local": "1", "Content-Type": content_type, **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        sys.exit(f"Error: {json.loads(exc.read() or b'{}').get('error', exc)}")
    except urllib.error.URLError:
        start = f'"{sys.executable}"' if getattr(sys, "frozen", False) else "python3 sn_clipboard.py"
        sys.exit(f"Supernote Clipboard is not running. Start it first with: {start}")


def main():
    parser = argparse.ArgumentParser(description=f"{APP_NAME}: send screenshots to your Supernote.")
    sub = parser.add_subparsers(dest="command")
    serve_parser = sub.add_parser("serve", help="run the app (default)")
    for p in (parser, serve_parser):
        p.add_argument("--port", type=int, help=f"port to listen on (default {DEFAULT_PORT})")
        p.add_argument("--no-browser", action="store_true", help="do not open the window in a browser")
        p.add_argument("--no-watch", action="store_true", help="do not import screenshots from the clipboard")
        p.add_argument("--keep-days", type=int, metavar="N",
                       help=f"delete items after N days, 0 = no limit (default {STORAGE_LIMITS['keep_seconds'][0] // 86400}). "
                            "Shorter times, such as 60s or 1h, are set in the window.")
        p.add_argument("--max-items", type=int, metavar="N",
                       help=f"keep only the newest N items, 0 = no limit (default {STORAGE_LIMITS['max_items'][0]})")
        p.add_argument("--max-mb", type=int, metavar="N",
                       help="delete the oldest items when they use more than N MB, 0 = no limit (default)")
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
        result = call_running_instance("/local/upload", args.file.read_bytes(), "application/octet-stream",
                                       {"X-SN-Filename": urllib.parse.quote(args.file.name)})
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
  .shot .text { align-items:flex-start; justify-content:flex-start; padding:12px; overflow:hidden;
                white-space:pre-wrap; word-break:break-word; font-size:13px; text-align:left; }
  select { font:inherit; padding:4px 6px; border-radius:6px; }
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
        Nothing to type on your Supernote: open a note and tap <b>Paste from PC</b>. The first time, it finds this
        computer on your Wi-Fi and a request appears here &mdash; click <b>Allow</b> once.
        <span id="manual"></span>
      </div>
    </div>
    <div class="devices" id="devices"></div>
  </section>

  <section class="card">
    <div class="row">
      <label class="toggle"><input type="checkbox" id="watch"> Import copied images</label>
      <label class="toggle"><input type="checkbox" id="watchText"> Import copied text</label>
    </div>
    <div class="hint" style="margin-top:8px">Passwords copied from password managers are never imported.
      Nothing is sent anywhere except to your paired Supernote.</div>
    <div class="msg" id="msg"></div>
  </section>

  <section class="card">
    <div class="row">
      <b>Storage</b>
      <label class="toggle">Time limit <select id="keep_seconds"></select></label>
      <label class="toggle">Keep at most <select id="max_items"></select></label>
      <label class="toggle">Use at most <select id="max_mb"></select></label>
      <span style="flex:1"></span>
      <button id="clear" class="small">Clear all</button>
    </div>
    <div class="row" style="margin-top:12px">
      <label class="toggle"><input type="checkbox" id="deleteOnPaste"> Delete an item after the Supernote pastes it</label>
    </div>
    <div class="hint" style="margin-top:8px"><span id="usage"></span> Items are deleted after the time limit, or when pasted if that option is on. The note keeps its picture.</div>
  </section>

  <div id="drop">Drop images here (PNG, JPEG, WebP, HEIC, AVIF, GIF, SVG, TIFF, BMP, ...), or press
    <b>Ctrl/Cmd + V</b> to add an image or text from the clipboard.</div>

  <div class="grid" id="grid"></div>
  <div class="empty" id="empty" hidden>Nothing yet. Press the capture shortcut, click <b>Capture region</b>, or copy an image or some text.</div>
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

async function post(path, body, type, headers) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "X-SN-Local": "1", "Content-Type": type || "application/json", ...(headers || {}) },
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

function formatDuration(seconds) {
  if (seconds % 86400 === 0) {
    const days = seconds / 86400;
    return days === 1 ? "1 day" : `${days} days`;
  }
  if (seconds % 3600 === 0) {
    const hours = seconds / 3600;
    return hours === 1 ? "1 hour" : `${hours} hours`;
  }
  if (seconds % 60 === 0) {
    const minutes = seconds / 60;
    return minutes === 1 ? "1 minute" : `${minutes} minutes`;
  }
  return seconds === 1 ? "1 second" : `${seconds} seconds`;
}

function parseDuration(text) {
  const match = String(text).trim().match(/^(\d+)\s*(s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?|d|days?)?$/i);
  if (!match) return NaN;
  const count = parseInt(match[1], 10);
  const unit = (match[2] || "s").charAt(0).toLowerCase();
  const scale = unit === "d" ? 86400 : unit === "h" ? 3600 : unit === "m" ? 60 : 1;
  return count * scale;
}

// Storage limit choices; 0 = no limit. keep_seconds is the age, in seconds.
const LIMITS = {
  keep_seconds: {
    presets: [60, 120, 3600, 86400, 3 * 86400, 7 * 86400, 14 * 86400, 30 * 86400, 90 * 86400, 365 * 86400],
    label: formatDuration,
    parse: parseDuration,
    ask: "Delete items after how long? Examples: 60s, 2m, 1h, 7d. 0 = no limit.",
  },
  max_items: { presets: [10, 30, 50, 100, 200, 500, 1000], label: n => n === 1 ? "1 item" : `${n} items`,
               ask: "Keep at most how many items? (0 = no limit)" },
  max_mb: { presets: [50, 100, 250, 500, 1000, 5000], label: n => n >= 1000 && n % 1000 === 0 ? `${n / 1000} GB` : `${n} MB`,
            ask: "Use at most how many MB? (0 = no limit)" },
};

function fmtBytes(n) {
  if (n < 1024 * 1024) return `${Math.max(1, Math.round(n / 1024))} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}

function renderLimit(name, value) {
  const select = $(name);
  if (document.activeElement === select && select.dataset.value !== undefined) return;
  if (select.dataset.value === String(value)) return;
  const spec = LIMITS[name];
  const values = [...new Set([...spec.presets, value].filter(n => n > 0))].sort((a, b) => a - b);
  select.innerHTML = values.map(n => `<option value="${n}">${spec.label(n)}</option>`).join("")
    + '<option value="0">No limit</option><option value="custom">Custom...</option>';
  select.value = String(value);
  select.dataset.value = String(value);
}

function changeLimit(name) {
  const select = $(name);
  let value = select.value;
  if (value === "custom") {
    const answer = prompt(LIMITS[name].ask, LIMITS[name].parse ? "" : select.dataset.value);
    const parse = LIMITS[name].parse || (text => parseInt(text, 10));
    value = answer === null ? NaN : parse(answer);
    if (!(value >= 0)) { select.value = select.dataset.value; return; }
  }
  const limits = {};
  limits[name] = Number(value);
  delete select.dataset.value;
  post("/local/settings", { limits }).then(refresh).catch(e => alert(e.message));
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
  $("watchText").checked = state.watchText;
  $("deleteOnPaste").checked = !!state.deleteOnPaste;
  Object.keys(LIMITS).forEach(name => renderLimit(name, state.limits[name]));
  const count = state.shots.length;
  $("usage").textContent = `Using ${fmtBytes(state.usageBytes)} for ${count} item${count === 1 ? "" : "s"}.`;
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
    const isText = s.kind === "text";
    const preview = isText
      ? `<div class="thumb text">${esc(s.preview)}${s.chars > s.preview.length ? "&hellip;" : ""}</div>`
      : `<div class="thumb"><img loading="lazy" src="/local/shots/${s.id}.${s.ext}?maxWidth=480" alt=""></div>`;
    const size = isText ? `${s.chars} characters` : `${s.width} x ${s.height} px`;
    el.innerHTML = `
      ${preview}
      <div class="meta"><b>${fmtTime(s.createdAt)}</b>${i === 0 ? '<span class="badge">Next paste</span>' : ""}
        <br>${isText ? "Text" : "Image"} - ${size} - ${s.source}</div>
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

const IMAGE_FILE_RE = /\.(png|jpe?g|jpe|jfif|webp|heic|heif|hif|avif|gif|tiff?|bmp|dib|svg|psd|ico|jp2|j2k|tga)$/i;

function isImageFile(file) {
  return file.type.startsWith("image/") || IMAGE_FILE_RE.test(file.name || "");
}

// Fallback for formats the computer cannot convert but this browser can display.
function browserToPng(blob) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(blob);
    const img = new Image();
    img.onload = () => {
      const canvas = document.createElement("canvas");
      canvas.width = img.naturalWidth || 1200;
      canvas.height = img.naturalHeight || 900;
      canvas.getContext("2d").drawImage(img, 0, 0, canvas.width, canvas.height);
      URL.revokeObjectURL(url);
      canvas.toBlob(png => png ? resolve(png) : reject(new Error("conversion failed")), "image/png");
    };
    img.onerror = () => { URL.revokeObjectURL(url); reject(new Error("unreadable")); };
    img.src = url;
  });
}

async function upload(blob) {
  const name = blob.name || "";
  const headers = name ? { "X-SN-Filename": encodeURIComponent(name) } : {};
  try {
    await post("/local/upload", blob, blob.type || "application/octet-stream", headers);
  } catch (e) {
    const png = await browserToPng(blob).catch(() => null);
    if (!png) { alert(name ? `${name}: ${e.message}` : e.message); return; }
    await post("/local/upload", png, "image/png").catch(err => alert(err.message));
  }
  refresh();
}

$("capture").onclick = () => post("/local/capture").then(refresh);
$("watch").onchange = e => post("/local/settings", { watchClipboard: e.target.checked }).then(refresh);
$("watchText").onchange = e => post("/local/settings", { watchText: e.target.checked }).then(refresh);
$("deleteOnPaste").onchange = e => post("/local/settings", { deleteOnPaste: e.target.checked }).then(refresh);
Object.keys(LIMITS).forEach(name => $(name).onchange = () => changeLimit(name));
$("clear").onclick = () => confirm("Delete everything copied so far?") && post("/local/clear").then(refresh);
document.addEventListener("paste", e => {
  const files = [...e.clipboardData.files].filter(isImageFile);
  if (files.length) { e.preventDefault(); files.forEach(upload); return; }
  for (const item of e.clipboardData.items) {
    if (item.type.startsWith("image/")) { upload(item.getAsFile()); e.preventDefault(); return; }
  }
  const text = e.clipboardData.getData("text/plain");
  if (text.trim()) {
    e.preventDefault();
    post("/local/text", new Blob([text], { type: "text/plain;charset=utf-8" }), "text/plain;charset=utf-8")
      .then(refresh).catch(err => alert(err.message));
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
  const files = [...e.dataTransfer.files];
  const images = files.filter(isImageFile);
  images.forEach(upload);
  if (files.length > images.length) alert("Only images can be added (PNG, JPEG, WebP, HEIC, GIF, SVG, ...).");
});

refresh();
setInterval(refresh, 1500);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    main()
