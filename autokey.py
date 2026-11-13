"""AutoKey: paste a linked text file with a global hotkey.

The window starts hidden. Right-click the tray icon and choose Show App.
Hide Window (or the window's close button) hides it again without quitting.
Exit is only on the tray menu.

A hotkey copies that file's text into the focused box, then puts back whatever
was on the clipboard before the paste.

The Meetings tab signs in to Job Tracker. The server decides which interviews
in the next hour are due, and AutoKey shows those reminders.

Start it with run.bat. On a new computer, run.bat builds a local .venv
from whatever Python 3 is installed; do not copy .venv between machines.
"""

from __future__ import annotations

import ctypes
import json
import logging
import queue
import sys
import threading
import time
import tkinter as tk
import urllib.error
import urllib.parse
import urllib.request
import winreg
from ctypes import wintypes
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

try:
    import keyboard
except ImportError:
    ctypes.windll.user32.MessageBoxW(
        None,
        "The keyboard package is missing.\nDouble-click run.bat to set it up.",
        "AutoKey",
        0x10,
    )
    raise SystemExit(1)

APP_VERSION = "1.1.0"
APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "bindings.json"
LOG_PATH = APP_DIR / "autokey.log"
STARTUP_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_VALUE_NAME = "AutoKey"

PASTE_SETTLE_SECONDS = 0.30
CLIPBOARD_POLL_SECONDS = 0.12
CLIPBOARD_RETRIES = 25
MAX_FORMAT_BYTES = 64 * 1024 * 1024
DEFAULT_SERVER = "http://213.59.112.27:5000"
REMIND_HORIZON = timedelta(hours=1)
REMIND_SOON = timedelta(minutes=30)
REMIND_CLOSE = timedelta(minutes=15)
REMIND_EVERY = timedelta(minutes=15)
REMIND_SOON_EVERY = timedelta(minutes=10)
REMIND_CLOSE_EVERY = timedelta(minutes=5)
REMIND_POLL_SECONDS = 30

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_EXTENDEDKEY = 0x0001
ERROR_ALREADY_EXISTS = 183
MB_ICONINFORMATION = 0x40
MB_ICONERROR = 0x10

MODIFIER_KEYS = (
    (0xA0, False),  # VK_LSHIFT
    (0xA1, False),  # VK_RSHIFT
    (0xA2, False),  # VK_LCONTROL
    (0xA3, True),  # VK_RCONTROL
    (0xA4, False),  # VK_LMENU
    (0xA5, True),  # VK_RMENU
    (0x5B, True),  # VK_LWIN
    (0x5C, True),  # VK_RWIN
    (0x10, False),  # VK_SHIFT
    (0x11, False),  # VK_CONTROL
    (0x12, False),  # VK_MENU
)
MODIFIER_NAMES = {"ctrl", "alt", "shift", "windows", "alt gr"}
NAME_ALIASES = {
    "left menu": "left alt",
    "right menu": "right alt",
    "menu": "alt",
    "escape": "esc",
    "return": "enter",
    "spacebar": "space",
    "control": "ctrl",
    "win": "windows",
    "cmd": "windows",
}

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE
user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
user32.EnumClipboardFormats.restype = wintypes.UINT
user32.GetClipboardSequenceNumber.argtypes = []
user32.GetClipboardSequenceNumber.restype = wintypes.DWORD
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
user32.MapVirtualKeyW.restype = wintypes.UINT
user32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_ulonglong]
user32.keybd_event.restype = None
user32.MessageBoxW.argtypes = [wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT]
user32.MessageBoxW.restype = ctypes.c_int
user32.MessageBeep.argtypes = [wintypes.UINT]
user32.MessageBeep.restype = wintypes.BOOL

kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL
kernel32.GlobalSize.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalSize.restype = ctypes.c_size_t
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.restype = wintypes.HGLOBAL
kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.SetLastError.argtypes = [wintypes.DWORD]
kernel32.SetLastError.restype = None

_instance_mutex = None


def enable_dpi() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def message_box(text: str, title: str, error: bool = False) -> None:
    flags = MB_ICONERROR if error else MB_ICONINFORMATION
    user32.MessageBoxW(None, text, title, flags)


def setup_logging() -> None:
    handlers: list[logging.Handler] = []
    handlers.append(logging.FileHandler(LOG_PATH, encoding="utf-8"))
    if sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )


def acquire_single_instance() -> bool:
    global _instance_mutex
    kernel32.SetLastError(0)
    _instance_mutex = kernel32.CreateMutexW(None, False, "Local\\AutoKey.TextHotkeys")
    if not _instance_mutex:
        logging.error("CreateMutex failed: %s", ctypes.get_last_error())
        return True
    return ctypes.get_last_error() != ERROR_ALREADY_EXISTS


def startup_command() -> str:
    launcher = APP_DIR / "start_autokey.vbs"
    if launcher.is_file():
        return f'wscript.exe //nologo "{launcher}"'
    python_exe = APP_DIR / ".venv" / "Scripts" / "python.exe"
    script = APP_DIR / "autokey.py"
    if python_exe.is_file():
        return f'"{python_exe}" "{script}"'
    return f'"{APP_DIR / "run.bat"}"'


def is_our_startup_command(value: str) -> bool:
    marker = str(APP_DIR).casefold().replace("/", "\\")
    return marker in value.casefold().replace("/", "\\")


def read_startup_command(value_name: str = STARTUP_VALUE_NAME) -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_RUN_KEY, 0, winreg.KEY_READ) as key:
            value, _regtype = winreg.QueryValueEx(key, value_name)
    except FileNotFoundError:
        return None
    except OSError:
        return None
    return str(value)


def is_startup_enabled(value_name: str = STARTUP_VALUE_NAME) -> bool:
    value = read_startup_command(value_name)
    return bool(value) and is_our_startup_command(value)


def set_startup_enabled(enabled: bool, value_name: str = STARTUP_VALUE_NAME) -> None:
    if enabled:
        python_exe = APP_DIR / ".venv" / "Scripts" / "python.exe"
        if not (APP_DIR / "autokey.py").is_file() or not (
            python_exe.is_file() or (APP_DIR / "start_autokey.vbs").is_file() or (APP_DIR / "run.bat").is_file()
        ):
            raise FileNotFoundError("AutoKey could not find a program to start with Windows.")
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, STARTUP_RUN_KEY) as key:
            winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ, startup_command())
        return
    access = winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_RUN_KEY, 0, access) as key:
            current, _regtype = winreg.QueryValueEx(key, value_name)
            if is_our_startup_command(str(current)):
                winreg.DeleteValue(key, value_name)
    except FileNotFoundError:
        return


def _open_clipboard() -> bool:
    for _ in range(CLIPBOARD_RETRIES):
        if user32.OpenClipboard(None):
            return True
        time.sleep(0.01)
    return False


def _set_format(fmt: int, data: bytes) -> bool:
    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(data))
    if not handle:
        return False
    ptr = kernel32.GlobalLock(handle)
    if not ptr:
        kernel32.GlobalFree(handle)
        return False
    try:
        ctypes.memmove(ptr, data, len(data))
    finally:
        kernel32.GlobalUnlock(handle)
    if not user32.SetClipboardData(fmt, handle):
        kernel32.GlobalFree(handle)
        return False
    return True


def backup_clipboard() -> list[tuple[int, bytes]] | None:
    """Copy every memory-backed clipboard format. None means the clipboard was busy."""
    if not _open_clipboard():
        logging.error("Could not open the clipboard to save it")
        return None
    try:
        saved: list[tuple[int, bytes]] = []
        seen: set[int] = set()
        fmt = 0
        while True:
            fmt = user32.EnumClipboardFormats(fmt)
            if not fmt:
                break
            if fmt in seen:
                continue
            seen.add(fmt)
            handle = user32.GetClipboardData(fmt)
            if not handle:
                continue
            size = kernel32.GlobalSize(handle)
            if not size or size > MAX_FORMAT_BYTES:
                logging.info("Leaving clipboard format %s unrestored (size %s)", fmt, size)
                continue
            ptr = kernel32.GlobalLock(handle)
            if not ptr:
                continue
            try:
                saved.append((fmt, ctypes.string_at(ptr, size)))
            finally:
                kernel32.GlobalUnlock(handle)
        return saved
    finally:
        user32.CloseClipboard()


def set_clipboard_text(text: str) -> None:
    data = text.encode("utf-16-le") + b"\x00\x00"
    if not _open_clipboard():
        raise OSError("Could not open the clipboard")
    try:
        if not user32.EmptyClipboard():
            raise OSError("Could not empty the clipboard")
        if not _set_format(CF_UNICODETEXT, data):
            raise OSError("Could not place text on the clipboard")
    finally:
        user32.CloseClipboard()


def restore_clipboard(items: list[tuple[int, bytes]] | None) -> bool:
    if items is None:
        return False
    for attempt in range(8):
        if _restore_once(items):
            return True
        time.sleep(0.03 * (attempt + 1))
    logging.error("Could not restore the original clipboard")
    return False


def _restore_once(items: list[tuple[int, bytes]]) -> bool:
    if not _open_clipboard():
        return False
    try:
        if not user32.EmptyClipboard():
            return False
        seen: set[int] = set()
        for fmt, data in items:
            if fmt in seen:
                continue
            seen.add(fmt)
            if not _set_format(fmt, data):
                logging.warning("Could not restore clipboard format %s", fmt)
        return True
    finally:
        user32.CloseClipboard()


def get_clipboard_text() -> str:
    if not _open_clipboard():
        raise OSError("Could not open the clipboard")
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""
        size = kernel32.GlobalSize(handle)
        ptr = kernel32.GlobalLock(handle)
        if not ptr or not size:
            return ""
        try:
            raw = ctypes.string_at(ptr, size)
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()
    if len(raw) % 2:
        raw = raw[:-1]
    return raw.decode("utf-16-le", errors="surrogatepass").split("\0", 1)[0]


def read_text_file(path: str) -> str:
    data = Path(path).read_bytes()
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252")


def file_preview(path: str, limit: int = 72) -> str:
    try:
        text = read_text_file(path)
    except FileNotFoundError:
        return "(missing file)"
    except OSError:
        return "(unreadable)"
    compact = " ".join(text.split())
    if not compact:
        return "(empty)"
    if len(compact) > limit:
        return compact[: limit - 3] + "..."
    return compact


def release_modifiers() -> None:
    """Drop Ctrl/Alt/Shift/Win so the paste is not delivered as Alt+Ctrl+V."""
    for vk, extended in MODIFIER_KEYS:
        if user32.GetAsyncKeyState(vk) & 0x8000:
            scan = user32.MapVirtualKeyW(vk, 0) & 0xFF
            flags = KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if extended else 0)
            user32.keybd_event(vk, scan, flags, 0)


class ClipboardSilence:
    """Skip clipboard-transform while AutoKey itself is writing the clipboard."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._depth = 0

    def __enter__(self) -> ClipboardSilence:
        with self._lock:
            self._depth += 1
        return self

    def __exit__(self, *_exc) -> None:
        with self._lock:
            self._depth -= 1

    def active(self) -> bool:
        with self._lock:
            return self._depth > 0


CLIPBOARD_SILENCE = ClipboardSilence()


def transform_text(text: str, pairs: list[tuple[str, str]]) -> str:
    """Replace each source spell with its pair. Longer spells win; replacements do not chain."""
    mapping = {source: dest for source, dest in pairs if source}
    if not mapping or not text:
        return text
    sources = sorted(mapping, key=len, reverse=True)
    out: list[str] = []
    index = 0
    length = len(text)
    while index < length:
        matched = next((source for source in sources if text.startswith(source, index)), None)
        if matched is None:
            out.append(text[index])
            index += 1
            continue
        out.append(mapping[matched])
        index += len(matched)
    return "".join(out)


class ClipboardWatcher:
    """When enabled, rewrite clipboard text using the current spell pairs."""

    def __init__(self, settings, on_changed=None) -> None:
        self._settings = settings
        self._on_changed = on_changed
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_seq = 0

    def start(self) -> None:
        self._last_seq = user32.GetClipboardSequenceNumber()
        self._thread = threading.Thread(target=self._run, name="autokey-clipboard", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=2)

    def _run(self) -> None:
        while not self._stop.wait(CLIPBOARD_POLL_SECONDS):
            try:
                self._tick()
            except Exception:
                logging.exception("Clipboard transform failed")

    def _tick(self) -> None:
        seq = user32.GetClipboardSequenceNumber()
        if CLIPBOARD_SILENCE.active():
            self._last_seq = seq
            return
        if seq == self._last_seq:
            return
        self._last_seq = seq
        enabled, pairs = self._settings()
        if not enabled or not pairs:
            return
        if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
            return
        try:
            text = get_clipboard_text()
        except OSError:
            return
        updated = transform_text(text, pairs)
        if updated == text:
            return
        with CLIPBOARD_SILENCE:
            set_clipboard_text(updated)
            self._last_seq = user32.GetClipboardSequenceNumber()
        logging.info("Transformed clipboard text (%s -> %s characters)", len(text), len(updated))
        if self._on_changed is not None:
            try:
                self._on_changed()
            except Exception:
                logging.exception("Could not report clipboard transform")


def paste_text(text: str) -> None:
    saved = backup_clipboard()
    if saved is None:
        raise OSError("Clipboard is busy, so nothing was pasted")
    with CLIPBOARD_SILENCE:
        try:
            set_clipboard_text(text)
            release_modifiers()
            time.sleep(0.05)
            keyboard.send("ctrl+v")
            # The focused app reads the clipboard after the keystroke is queued.
            time.sleep(PASTE_SETTLE_SECONDS)
        finally:
            if not restore_clipboard(saved):
                raise OSError("The text was sent, but the original clipboard could not be restored")


def canonical_key_name(name: str | None) -> str:
    if not name:
        return ""
    raw = name
    try:
        name = keyboard.normalize_name(name)
    except ValueError:
        name = raw.lower()
    name = NAME_ALIASES.get(name, name)
    for prefix in ("left ", "right "):
        if name.startswith(prefix):
            name = name[len(prefix) :]
    name = NAME_ALIASES.get(name, name)
    if len(name) == 1:
        name = name.lower()
    return name


def canonical_hotkey(names: list[str]) -> str:
    cleaned = [canonical_key_name(name) for name in names if name]
    cleaned = [name for name in cleaned if name]
    if not cleaned:
        return ""
    return keyboard.get_hotkey_name(cleaned)


def normalize_hotkey(text: str) -> str:
    parts = [canonical_key_name(part.strip()) for part in text.split("+")]
    parts = [part for part in parts if part]
    if not parts:
        return ""
    return keyboard.get_hotkey_name(parts)


def is_bindable(hotkey: str) -> bool:
    parts = [part for part in hotkey.split("+") if part]
    return any(part not in MODIFIER_NAMES for part in parts)


def split_hotkey(hotkey: str) -> tuple[frozenset[str], str]:
    parts = [part for part in normalize_hotkey(hotkey).split("+") if part]
    mods: set[str] = set()
    keys: list[str] = []
    for part in parts:
        if part == "alt gr":
            mods.update({"ctrl", "alt"})
        elif part in MODIFIER_NAMES:
            mods.add(part)
        else:
            keys.append(part)
    if len(keys) != 1:
        raise ValueError(f"Hotkey must include one non-modifier key: {hotkey}")
    return frozenset(mods), keys[0]


def current_modifiers() -> frozenset[str]:
    mods: set[str] = set()
    if user32.GetAsyncKeyState(0x11) & 0x8000:
        mods.add("ctrl")
    if user32.GetAsyncKeyState(0x10) & 0x8000:
        mods.add("shift")
    if user32.GetAsyncKeyState(0x12) & 0x8000:
        mods.add("alt")
    if user32.GetAsyncKeyState(0x5B) & 0x8000 or user32.GetAsyncKeyState(0x5C) & 0x8000:
        mods.add("windows")
    return frozenset(mods)


def hotkey_matches(hotkey: str, pressed_mods: frozenset[str], key: str) -> bool:
    mods, trigger = split_hotkey(hotkey)
    return pressed_mods == mods and canonical_key_name(key) == trigger


class HotkeyRouter:
    """Fire a binding only when its modifiers match exactly.

    keyboard.add_hotkey('ctrl+shift+a') also fires on Ctrl+A. This hook checks
    the real modifier keys so Shift is required when it is part of the combo.
    """

    def __init__(self, on_fire) -> None:
        self._on_fire = on_fire
        self._bindings: dict[tuple[frozenset[str], str], str] = {}
        self._hook = None
        self._suppress_key = ""

    def set_bindings(self, items: list[tuple[str, str]]) -> list[str]:
        self.stop()
        failed: list[str] = []
        seen: set[tuple[frozenset[str], str]] = set()
        for hotkey, path in items:
            try:
                combo = split_hotkey(hotkey)
            except Exception:
                logging.exception("Could not register %s", hotkey)
                failed.append(pretty_hotkey(hotkey))
                continue
            if combo in seen:
                failed.append(pretty_hotkey(hotkey))
                continue
            seen.add(combo)
            self._bindings[combo] = path
        if self._bindings:
            self._hook = keyboard.hook(self._on_event, suppress=True)
        return failed

    def stop(self) -> None:
        hook = self._hook
        self._hook = None
        self._bindings = {}
        self._suppress_key = ""
        if hook is not None:
            try:
                keyboard.unhook(hook)
            except KeyError:
                pass

    def _on_event(self, event) -> bool:
        name = canonical_key_name(event.name)
        if not name:
            return True
        if name in MODIFIER_NAMES:
            return True
        if event.event_type == keyboard.KEY_DOWN:
            if self._suppress_key == name:
                return False
            path = self._bindings.get((current_modifiers(), name))
            if path is None:
                return True
            self._suppress_key = name
            self._on_fire(path)
            return False
        if self._suppress_key == name:
            self._suppress_key = ""
            return False
        return True


def pretty_hotkey(hotkey: str) -> str:
    names = {
        "ctrl": "Ctrl",
        "alt": "Alt",
        "shift": "Shift",
        "windows": "Win",
        "alt gr": "AltGr",
        "enter": "Enter",
        "esc": "Esc",
        "space": "Space",
        "tab": "Tab",
        "backspace": "Backspace",
        "delete": "Delete",
        "insert": "Insert",
        "home": "Home",
        "end": "End",
        "page up": "Page Up",
        "page down": "Page Down",
        "up": "Up",
        "down": "Down",
        "left": "Left",
        "right": "Right",
        "caps lock": "Caps Lock",
        "num lock": "Num Lock",
        "scroll lock": "Scroll Lock",
        "print screen": "Print Screen",
        "plus": "Plus",
        "comma": "Comma",
    }
    shown = []
    for part in hotkey.split("+"):
        if part in names:
            shown.append(names[part])
        elif len(part) == 1:
            shown.append(part.upper())
        elif part.startswith("f") and part[1:].isdigit():
            shown.append(part.upper())
        else:
            shown.append(part.title())
    return " + ".join(shown)


def make_icon():
    from PIL import Image, ImageDraw, ImageFont

    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((1, 1, size - 2, size - 2), radius=14, fill=(37, 99, 235, 255))
    font = None
    for face in ("segoeuib.ttf", "arialbd.ttf"):
        try:
            font = ImageFont.truetype(face, 36)
            break
        except OSError:
            continue
    if font is None:
        font = ImageFont.load_default()
    text = "A"
    bbox = draw.textbbox((0, 0), text, font=font)
    x = (size - (bbox[2] - bbox[0])) / 2 - bbox[0]
    y = (size - (bbox[3] - bbox[1])) / 2 - bbox[1] - 1
    draw.text((x, y), text, font=font, fill=(255, 255, 255, 255))
    return image


class HotkeyCapture:
    """Listen for one key combination. Esc cancels. Caller must stop() from the UI thread."""

    def __init__(self, schedule, on_hotkey, on_cancel, on_status) -> None:
        self._schedule = schedule
        self._on_hotkey = on_hotkey
        self._on_cancel = on_cancel
        self._on_status = on_status
        self._done = False
        self._pressed: list[str] = []
        self._best: list[str] = []
        self._hook = None

    def start(self) -> None:
        self._on_status("Press the keys, then release them. Esc cancels.")
        self._hook = keyboard.hook(self._on_event, suppress=True)

    def stop(self) -> None:
        self._done = True
        hook = self._hook
        self._hook = None
        if hook is not None:
            try:
                keyboard.unhook(hook)
            except KeyError:
                pass

    def _on_event(self, event) -> bool:
        if self._done or not event.name:
            return False
        name = event.name
        if event.event_type == keyboard.KEY_DOWN:
            if name in ("esc", "escape") and not any(key not in ("esc", "escape") for key in self._pressed):
                self._done = True
                self._schedule(self._cancel)
                return False
            if name not in self._pressed:
                self._pressed.append(name)
            self._best = list(self._pressed)
            return False
        if name in self._pressed:
            self._pressed.remove(name)
        if self._pressed or not self._best:
            return False
        hotkey = canonical_hotkey(self._best)
        self._best = []
        if not is_bindable(hotkey):
            self._schedule(lambda: self._on_status("Include a key other than Ctrl, Alt, Shift, or Win."))
            return False
        self._done = True
        self._schedule(lambda: self._finish(hotkey))
        return False

    def _finish(self, hotkey: str) -> None:
        self.stop()
        self._on_hotkey(hotkey)

    def _cancel(self) -> None:
        self.stop()
        self._on_cancel()


class BindingDialog(tk.Toplevel):
    def __init__(self, master: tk.Tk, current: tuple[str, str] | None, taken: set[str]) -> None:
        super().__init__(master)
        self.result: tuple[str, str] | None = None
        self._taken = taken
        self._capture: HotkeyCapture | None = None
        self.title("Edit Hotkey" if current else "Add Hotkey")
        self.resizable(False, False)
        self.transient(master)
        self.configure(padx=16, pady=14)

        ttk.Label(self, text="Hotkey").grid(row=0, column=0, sticky="w")
        self.hotkey_var = tk.StringVar(value=current[0] if current else "")
        self.hotkey_entry = ttk.Entry(self, textvariable=self.hotkey_var, width=36)
        self.hotkey_entry.grid(row=1, column=0, sticky="we", pady=(4, 0))
        self.record_button = ttk.Button(self, text="Record", command=self._toggle_record, width=10)
        self.record_button.grid(row=1, column=1, padx=(8, 0), pady=(4, 0))

        self.hint_var = tk.StringVar(value="Click Record and press the keys, or type a combo such as ctrl+alt+1.")
        ttk.Label(self, textvariable=self.hint_var, style="Hint.TLabel", wraplength=420).grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(6, 12)
        )

        ttk.Label(self, text="Text file").grid(row=3, column=0, sticky="w")
        self.file_var = tk.StringVar(value=current[1] if current else "")
        ttk.Entry(self, textvariable=self.file_var, width=36).grid(row=4, column=0, sticky="we", pady=(4, 0))
        ttk.Button(self, text="Browse", command=self._browse, width=10).grid(row=4, column=1, padx=(8, 0), pady=(4, 0))

        actions = ttk.Frame(self)
        actions.grid(row=5, column=0, columnspan=2, sticky="e", pady=(16, 0))
        ttk.Button(actions, text="Cancel", command=self._cancel, width=10).pack(side="right")
        ttk.Button(actions, text="Save", command=self._save, width=10).pack(side="right", padx=(0, 8))

        self.columnconfigure(0, weight=1)
        self.bind("<Escape>", lambda _event: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.grab_set()
        self.after(10, self._center)

    def _center(self) -> None:
        self.update_idletasks()
        width = self.winfo_width()
        height = self.winfo_height()
        x = (self.winfo_screenwidth() - width) // 2
        y = (self.winfo_screenheight() - height) // 2
        self.geometry(f"+{x}+{y}")

    def _toggle_record(self) -> None:
        if self._capture is not None:
            self._capture.stop()
            self._capture = None
            self.record_button.configure(text="Record")
            self.hotkey_entry.configure(state="normal")
            self.hint_var.set("Recording cancelled.")
            return
        self.record_button.configure(text="Cancel")
        self.hotkey_entry.configure(state="disabled")
        self._capture = HotkeyCapture(self.after_idle_call, self._recorded, self._record_cancelled, self.hint_var.set)
        self._capture.start()

    def after_idle_call(self, callback) -> None:
        try:
            self.after(0, callback)
        except tk.TclError:
            pass

    def _recorded(self, hotkey: str) -> None:
        self._capture = None
        self.record_button.configure(text="Record")
        self.hotkey_entry.configure(state="normal")
        self.hotkey_var.set(hotkey)
        self.hint_var.set(f"Recorded {pretty_hotkey(hotkey)}.")

    def _record_cancelled(self) -> None:
        self._capture = None
        self.record_button.configure(text="Record")
        self.hotkey_entry.configure(state="normal")
        self.hint_var.set("Recording cancelled.")

    def _browse(self) -> None:
        current = self.file_var.get().strip()
        initial_path = Path(current).expanduser().parent if current else APP_DIR
        initial = str(initial_path if initial_path.is_dir() else APP_DIR)
        chosen = filedialog.askopenfilename(
            parent=self,
            title="Select a text file",
            initialdir=initial,
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
        )
        if chosen:
            self.file_var.set(chosen)

    def _save(self) -> None:
        if self._capture is not None:
            self.hint_var.set("Finish or cancel recording first.")
            return
        raw = self.hotkey_var.get().strip()
        try:
            hotkey = normalize_hotkey(raw)
            keyboard.parse_hotkey(hotkey)
        except Exception:
            messagebox.showerror("AutoKey", "That hotkey is not valid.", parent=self)
            return
        if not is_bindable(hotkey):
            messagebox.showerror("AutoKey", "Choose a key other than only Ctrl, Alt, Shift, or Win.", parent=self)
            return
        if hotkey in self._taken:
            messagebox.showerror("AutoKey", f"{pretty_hotkey(hotkey)} is already linked.", parent=self)
            return
        file_text = self.file_var.get().strip()
        if not file_text:
            messagebox.showerror("AutoKey", "Choose a text file.", parent=self)
            return
        path = Path(file_text).expanduser()
        if not path.is_file():
            messagebox.showerror("AutoKey", "That text file does not exist.", parent=self)
            return
        self.result = (hotkey, str(path.resolve()))
        self._close()

    def _cancel(self) -> None:
        self.result = None
        self._close()

    def _close(self) -> None:
        if self._capture is not None:
            self._capture.stop()
            self._capture = None
        self.grab_release()
        self.destroy()


class PairDialog(tk.Toplevel):
    def __init__(self, master: tk.Tk, current: tuple[str, str] | None, taken: set[str]) -> None:
        super().__init__(master)
        self.result: tuple[str, str] | None = None
        self._taken = taken
        self.title("Edit Spell" if current else "Add Spell")
        self.resizable(False, False)
        self.transient(master)
        self.configure(padx=16, pady=14)

        ttk.Label(self, text="Spell on the clipboard").grid(row=0, column=0, sticky="w")
        self.source_var = tk.StringVar(value=current[0] if current else "")
        ttk.Entry(self, textvariable=self.source_var, width=36).grid(row=1, column=0, sticky="we", pady=(4, 10))

        ttk.Label(self, text="Replace with").grid(row=2, column=0, sticky="w")
        self.dest_var = tk.StringVar(value=current[1] if current else "")
        ttk.Entry(self, textvariable=self.dest_var, width=36).grid(row=3, column=0, sticky="we", pady=(4, 0))
        ttk.Label(
            self,
            text="Example: A becomes B. When the clipboard changes, every matching spell is replaced.",
            style="Hint.TLabel",
            wraplength=420,
        ).grid(row=4, column=0, sticky="w", pady=(8, 0))

        actions = ttk.Frame(self)
        actions.grid(row=5, column=0, sticky="e", pady=(16, 0))
        ttk.Button(actions, text="Cancel", command=self._cancel, width=10).pack(side="right")
        ttk.Button(actions, text="Save", command=self._save, width=10).pack(side="right", padx=(0, 8))

        self.columnconfigure(0, weight=1)
        self.bind("<Escape>", lambda _event: self._cancel())
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.grab_set()
        self.after(10, self._center)

    def _center(self) -> None:
        self.update_idletasks()
        width = self.winfo_width()
        height = self.winfo_height()
        x = (self.winfo_screenwidth() - width) // 2
        y = (self.winfo_screenheight() - height) // 2
        self.geometry(f"+{x}+{y}")

    def _save(self) -> None:
        source = self.source_var.get()
        dest = self.dest_var.get()
        if not source:
            messagebox.showerror("AutoKey", "Enter the spell to look for.", parent=self)
            return
        if source in self._taken:
            messagebox.showerror("AutoKey", "That spell is already in the list.", parent=self)
            return
        self.result = (source, dest)
        self.destroy()

    def _cancel(self) -> None:
        self.result = None
        self.destroy()


def api_root(raw: str) -> str:
    """Turn a server address into the API root. A pasted path is dropped."""
    origin = server_origin(raw)
    return f"{origin}/api" if origin else ""


def server_origin(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    parsed = urllib.parse.urlsplit(text)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def interview_step_label(step: int) -> str:
    try:
        number = int(step)
    except (TypeError, ValueError):
        number = 1
    if number < 1:
        number = 1
    suffix = "th"
    mod100 = number % 100
    if mod100 < 11 or mod100 > 13:
        last = number % 10
        if last == 1:
            suffix = "st"
        elif last == 2:
            suffix = "nd"
        elif last == 3:
            suffix = "rd"
    return f"{number}{suffix} interview"


def parse_server_time(value) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=datetime.now().astimezone().tzinfo)
    return parsed


def meeting_start(meeting: dict) -> datetime | None:
    interview = meeting.get("interview") or {}
    return parse_server_time(interview.get("startsAt") or meeting.get("startsAt"))


def meeting_key(meeting: dict) -> str:
    interview = meeting.get("interview") or {}
    ident = interview.get("_id") or interview.get("id")
    if ident:
        return str(ident)
    return f"{meeting.get('jobId')}:{interview.get('startsAt')}"


def interviewer_label(meeting: dict) -> str:
    direct = str(meeting.get("interviewer") or "").strip()
    if direct:
        return direct
    interview = meeting.get("interview") or {}
    names = []
    for person in interview.get("interviewers") or []:
        name = str((person or {}).get("name") or "").strip()
        if name:
            names.append(name)
    return ", ".join(names) if names else "Not set"


def reminder_gap(until_start: timedelta) -> timedelta | None:
    """How long to wait before reminding again. None means this meeting is outside the window."""
    if until_start <= timedelta(0) or until_start > REMIND_HORIZON:
        return None
    if until_start <= REMIND_CLOSE:
        return REMIND_CLOSE_EVERY
    if until_start <= REMIND_SOON:
        return REMIND_SOON_EVERY
    return REMIND_EVERY


def due_reminders(meetings: list[dict], last_notified: dict[str, datetime], now: datetime) -> list[dict]:
    """Meetings that should pop up now. Drops remembered meetings that left the window."""
    due = []
    active: set[str] = set()
    for meeting in meetings:
        start = meeting_start(meeting)
        if start is None:
            continue
        gap = reminder_gap(start - now)
        if gap is None:
            continue
        key = meeting_key(meeting)
        active.add(key)
        previous = last_notified.get(key)
        if previous is not None and now - previous < gap:
            continue
        due.append(meeting)
    for key in list(last_notified):
        if key not in active:
            del last_notified[key]
    due.sort(key=lambda item: meeting_start(item) or now)
    return due


def format_local_time(when: datetime) -> str:
    local = when.astimezone()
    clock = local.strftime("%I:%M %p").lstrip("0")
    return f"{local.strftime('%a %b')} {local.day}, {clock}"


def remaining_label(start: datetime, now: datetime) -> str:
    minutes = int((start - now).total_seconds() // 60)
    if minutes <= 0:
        return "less than 1 minute"
    if minutes == 1:
        return "1 minute"
    return f"{minutes} minutes"


def meeting_alert_text(meeting: dict, now: datetime) -> str:
    start = meeting_start(meeting)
    company = str(meeting.get("companyName") or "").strip() or "Company"
    title = str(meeting.get("jobTitle") or "").strip()
    lines = [
        company,
        str(meeting.get("stepLabel") or "").strip() or interview_step_label(meeting.get("step") or 1),
        f"Interviewer: {interviewer_label(meeting)}",
    ]
    if title:
        lines.append(title)
    if start is not None:
        lines.append(format_local_time(start))
        remaining = str(meeting.get("remainingLabel") or "").strip() or remaining_label(start, now)
        lines.append(f"Remaining: {remaining}")
    return "\n".join(lines)


class TrackerError(Exception):
    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


class JobTrackerClient:
    """Signs in to Job Tracker and reads the calendar. The token is kept in memory."""

    def __init__(self, server: str, email: str, password: str, token: str = "") -> None:
        self.root = api_root(server)
        self.email = (email or "").strip()
        self.password = password or ""
        self.token = token or ""

    def reminders(self) -> dict:
        payload = self._authorized("GET", "/autokey/reminders")
        due = payload.get("due") if isinstance(payload, dict) else None
        upcoming = payload.get("upcoming") if isinstance(payload, dict) else None
        return {
            "due": list(due) if isinstance(due, list) else [],
            "upcoming": list(upcoming) if isinstance(upcoming, list) else [],
        }

    def acknowledge(self, interview_ids: list[str]) -> None:
        ids = [str(item) for item in interview_ids if item]
        if ids:
            self._authorized("POST", "/autokey/reminders/ack", {"interviewIds": ids})

    def fetch_config(self) -> dict:
        payload = self._authorized("GET", "/autokey/config")
        return payload if isinstance(payload, dict) else {}

    def save_config(self, config: dict) -> dict:
        payload = self._authorized("PUT", "/autokey/config", config)
        return payload if isinstance(payload, dict) else {}

    def import_config(self, config: dict) -> dict:
        payload = self._authorized("POST", "/autokey/config/import", config)
        return payload if isinstance(payload, dict) else {}

    def _authorized(self, method: str, path: str, body: dict | None = None) -> dict:
        if not self.root:
            raise TrackerError("Enter the Job Tracker server address.")
        if not self.token:
            self.login()
        try:
            return self._request(method, path, body)
        except TrackerError as exc:
            if exc.status != 401:
                raise
            self.login()
            return self._request(method, path, body)

    def login(self) -> str:
        if not self.email or not self.password:
            raise TrackerError("Enter the Job Tracker email and password.")
        payload = self._request(
            "POST",
            "/auth/login",
            {"email": self.email, "password": self.password},
            auth=False,
        )
        token = payload.get("token") if isinstance(payload, dict) else None
        if not token:
            raise TrackerError("Job Tracker did not return a sign-in token.")
        self.token = str(token)
        return self.token

    def _request(self, method: str, path: str, body: dict | None = None, auth: bool = True) -> dict:
        data = None if body is None else json.dumps(body).encode("utf-8")
        headers = {"Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        if auth:
            if not self.token:
                raise TrackerError("Not signed in.", 401)
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(self.root + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                parsed = json.loads(exc.read().decode("utf-8"))
                detail = str(parsed.get("error") or parsed.get("message") or "")
            except Exception:
                detail = ""
            raise TrackerError(detail or f"Job Tracker returned {exc.code}.", exc.code) from exc
        except urllib.error.URLError as exc:
            raise TrackerError("Could not reach Job Tracker.") from exc
        if not raw:
            return {}
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}


class MeetingReminder:
    """Polls Job Tracker and asks the app to pop up when a reminder is due."""

    def __init__(self, settings, on_due, on_status, on_token=None) -> None:
        self._settings = settings
        self._on_due = on_due
        self._on_status = on_status
        self._on_token = on_token
        self._token = ""
        self._stop = threading.Event()
        self._check_lock = threading.Lock()
        self._thread = threading.Thread(target=self._loop, name="autokey-meetings", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def forget(self) -> None:
        self._token = ""

    def use_token(self, token: str) -> None:
        self._token = token or ""

    def check_soon(self) -> None:
        threading.Thread(target=self._check, name="autokey-meetings-now", daemon=True).start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._check()
            self._stop.wait(REMIND_POLL_SECONDS)

    def _check(self) -> None:
        if not self._check_lock.acquire(blocking=False):
            return
        try:
            settings = self._settings()
            server = settings.get("server") or ""
            email = settings.get("email") or ""
            password = settings.get("password") or ""
            if not api_root(server) or not email or not password:
                return
            saved_token = settings.get("token") or ""
            client = JobTrackerClient(server, email, password, self._token or saved_token)
            try:
                report = client.reminders()
            except TrackerError as exc:
                logging.warning("Meeting check failed: %s", exc)
                self._on_status(str(exc))
                return
            except Exception:
                logging.exception("Meeting check failed")
                self._on_status("Could not check meetings.")
                return
            previous = self._token or saved_token
            self._token = client.token
            if self._on_token and client.token and client.token != previous:
                self._on_token(client.token)
            due = report["due"]
            upcoming = len(report["upcoming"])
            if due:
                self._on_due(due, datetime.now(timezone.utc), lambda ids: self._acknowledge(client, ids))
            if upcoming == 1:
                self._on_status("Watching 1 interview in the next hour.")
            elif upcoming:
                self._on_status(f"Watching {upcoming} interviews in the next hour.")
            else:
                self._on_status("No interviews in the next hour.")
        finally:
            self._check_lock.release()

    def _acknowledge(self, client: JobTrackerClient, interview_ids: list[str]) -> None:
        def work() -> None:
            try:
                client.acknowledge(interview_ids)
            except Exception:
                logging.exception("Could not tell Job Tracker the reminder was shown")

        threading.Thread(target=work, name="autokey-ack", daemon=True).start()


class MeetingAlert(tk.Toplevel):
    def __init__(self, master: tk.Misc, text: str) -> None:
        super().__init__(master)
        self.title("Upcoming interview")
        self.attributes("-topmost", True)
        self.resizable(False, False)
        self.configure(padx=16, pady=14)
        self.body = tk.Label(
            self,
            text=text,
            justify="left",
            anchor="nw",
            font=("Segoe UI", 11),
            wraplength=420,
        )
        self.body.pack(anchor="w")
        ttk.Button(self, text="Dismiss", command=self.destroy, width=12).pack(anchor="e", pady=(12, 0))
        self._place()
        self._beep()

    def refresh(self, text: str) -> None:
        self.body.configure(text=text)
        self.attributes("-topmost", True)
        self.deiconify()
        self._place()
        self.lift()
        self._beep()

    def _place(self) -> None:
        self.update_idletasks()
        width = max(self.winfo_reqwidth(), 360)
        height = self.winfo_reqheight()
        x = max(0, self.winfo_screenwidth() - width - 24)
        y = max(0, self.winfo_screenheight() - height - 80)
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _beep(self) -> None:
        try:
            user32.MessageBeep(MB_ICONINFORMATION)
        except Exception:
            logging.exception("Could not play the reminder sound")


class App:
    def __init__(self) -> None:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("AutoKey.TextHotkeys")
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.attributes("-alpha", 0.0)
        self.root.title("AutoKey")
        self.root.minsize(760, 520)
        self.root.geometry("860x560")
        self._center(self.root, 860, 560)
        self._quitting = False
        self._bindings: list[tuple[str, str]] = []
        self._pairs: list[tuple[str, str]] = []
        self._pair_lock = threading.Lock()
        self._transform_on = False
        self._router = HotkeyRouter(self._enqueue)
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._worker = threading.Thread(target=self._paste_loop, name="autokey-paste", daemon=True)
        self._watcher = ClipboardWatcher(self._transform_settings, self._on_clipboard_transformed)
        self.icon = None
        self._photo = None
        self.status = tk.StringVar(value="No hotkeys yet.")
        self.start_with_windows = tk.BooleanVar(value=False)
        self.transform_enabled = tk.BooleanVar(value=False)
        self.server_var = tk.StringVar(value=DEFAULT_SERVER)
        self.email_var = tk.StringVar(value="")
        self.password_var = tk.StringVar(value="")
        self.meeting_status = tk.StringVar(value="Sign in to watch interviews.")
        self.account_status = tk.StringVar(value="Not signed in.")
        self._tracker_lock = threading.Lock()
        self._tracker = {"server": DEFAULT_SERVER, "email": "", "password": "", "token": "", "name": ""}
        self._alert: MeetingAlert | None = None
        self._apply_style()
        self._build()
        self._load()
        self._sync_startup_checkbox()
        self._refresh_rows()
        self._refresh_pairs()
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        self._worker.start()
        self._watcher.start()
        self._reminders = MeetingReminder(
            self._tracker_settings,
            self._on_meetings_due,
            self._on_meeting_status,
            self._on_tracker_token,
        )
        self._reminders.use_token(self._tracker_settings().get("token") or "")
        self._reminders.start()

    def _center(self, window: tk.Misc, width: int, height: int) -> None:
        window.update_idletasks()
        x = (window.winfo_screenwidth() - width) // 2
        y = (window.winfo_screenheight() - height) // 2
        window.geometry(f"{width}x{height}+{x}+{y}")

    def _apply_style(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("TLabel", font=("Segoe UI", 10))
        style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))
        style.configure("Hint.TLabel", font=("Segoe UI", 10), foreground="#555555")
        style.configure("Status.TLabel", font=("Segoe UI", 9), foreground="#444444")
        style.configure("Treeview", font=("Segoe UI", 10), rowheight=28)
        style.configure("Treeview.Heading", font=("Segoe UI", 10, "bold"))

    def _build(self) -> None:
        from PIL import ImageTk

        image = make_icon()
        self._photo = ImageTk.PhotoImage(image)
        self.root.iconphoto(True, self._photo)
        self._icon_image = image

        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="AutoKey", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="Link a hotkey to a text file. Pressing it pastes that file into the box you are typing in, then puts your clipboard back.",
            style="Hint.TLabel",
            wraplength=800,
        ).pack(anchor="w", pady=(4, 2))
        ttk.Label(
            outer,
            text="Closing this window hides it and leaves the hotkeys running. Choose Exit on the tray icon to quit.",
            style="Hint.TLabel",
            wraplength=800,
        ).pack(anchor="w", pady=(0, 2))
        ttk.Label(
            outer,
            text="Hotkeys and clipboard pairs are stored on your Job Tracker account. Export and import move that configuration as a file.",
            style="Hint.TLabel",
            wraplength=800,
        ).pack(anchor="w", pady=(0, 12))

        footer = ttk.Frame(outer)
        footer.pack(side="bottom", fill="x", pady=(12, 0))
        ttk.Button(footer, text="Hide Window", command=self.hide_window, width=16).pack(side="right")
        config_buttons = ttk.Frame(footer)
        config_buttons.pack(anchor="w")
        ttk.Button(config_buttons, text="Export configuration", command=self.export_configuration, width=22).pack(side="left")
        ttk.Button(config_buttons, text="Import configuration", command=self.import_configuration, width=22).pack(
            side="left", padx=(8, 0)
        )
        tk.Checkbutton(
            footer,
            text="Start AutoKey when Windows starts",
            variable=self.start_with_windows,
            command=self._on_startup_toggle,
            font=("Segoe UI", 10),
            anchor="w",
        ).pack(anchor="w")
        ttk.Label(footer, textvariable=self.status, style="Status.TLabel").pack(anchor="w", pady=(4, 0))

        self.notebook = ttk.Notebook(outer)
        self.notebook.pack(fill="both", expand=True)

        hotkeys = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(hotkeys, text="Hotkeys")
        table = ttk.Frame(hotkeys)
        table.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(table, columns=("hotkey", "file", "preview"), show="headings", selectmode="browse")
        self.tree.heading("hotkey", text="Hotkey")
        self.tree.heading("file", text="Text file")
        self.tree.heading("preview", text="Preview")
        self.tree.column("hotkey", width=170, minwidth=120, stretch=False)
        self.tree.column("file", width=390, minwidth=180, stretch=True)
        self.tree.column("preview", width=230, minwidth=120, stretch=True)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _event: self.edit_binding())
        hotkey_buttons = ttk.Frame(hotkeys)
        hotkey_buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(hotkey_buttons, text="Add", command=self.add_binding, width=10).pack(side="left")
        ttk.Button(hotkey_buttons, text="Edit", command=self.edit_binding, width=10).pack(side="left", padx=(8, 0))
        ttk.Button(hotkey_buttons, text="Remove", command=self.remove_binding, width=10).pack(side="left", padx=(8, 0))

        transform = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(transform, text="Clipboard transform")
        tk.Checkbutton(
            transform,
            text="Enable clipboard transformation",
            variable=self.transform_enabled,
            command=self._on_transform_toggle,
            font=("Segoe UI", 10),
            anchor="w",
        ).pack(anchor="w")
        ttk.Label(
            transform,
            text="Add spell pairs such as A → B. When this is on, new clipboard text is rewritten using those pairs.",
            style="Hint.TLabel",
            wraplength=760,
        ).pack(anchor="w", pady=(2, 8))
        pair_table = ttk.Frame(transform)
        pair_table.pack(fill="both", expand=True)
        self.pair_tree = ttk.Treeview(pair_table, columns=("source", "dest"), show="headings", selectmode="browse")
        self.pair_tree.heading("source", text="Spell")
        self.pair_tree.heading("dest", text="Becomes")
        self.pair_tree.column("source", width=360, minwidth=140, stretch=True)
        self.pair_tree.column("dest", width=360, minwidth=140, stretch=True)
        pair_scroll = ttk.Scrollbar(pair_table, orient="vertical", command=self.pair_tree.yview)
        self.pair_tree.configure(yscrollcommand=pair_scroll.set)
        self.pair_tree.pack(side="left", fill="both", expand=True)
        pair_scroll.pack(side="right", fill="y")
        self.pair_tree.bind("<Double-1>", lambda _event: self.edit_pair())
        pair_buttons = ttk.Frame(transform)
        pair_buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(pair_buttons, text="Add", command=self.add_pair, width=10).pack(side="left")
        ttk.Button(pair_buttons, text="Edit", command=self.edit_pair, width=10).pack(side="left", padx=(8, 0))
        ttk.Button(pair_buttons, text="Remove", command=self.remove_pair, width=10).pack(side="left", padx=(8, 0))

        meetings = ttk.Frame(self.notebook, padding=8)
        self.notebook.add(meetings, text="Meetings")
        self._meetings_tab = meetings
        ttk.Label(
            meetings,
            text="Sign in with the Job Tracker account. AutoKey then watches interviews that start within the next hour.",
            style="Hint.TLabel",
            wraplength=760,
        ).pack(anchor="w", pady=(0, 12))
        form = ttk.Frame(meetings)
        form.pack(anchor="w", fill="x")
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text="Server").grid(row=0, column=0, sticky="w", pady=4)
        ttk.Entry(form, textvariable=self.server_var, width=48).grid(row=0, column=1, sticky="ew", padx=(12, 0), pady=4)
        ttk.Label(form, text="Email").grid(row=1, column=0, sticky="w", pady=4)
        ttk.Entry(form, textvariable=self.email_var, width=48).grid(row=1, column=1, sticky="ew", padx=(12, 0), pady=4)
        ttk.Label(form, text="Password").grid(row=2, column=0, sticky="w", pady=4)
        ttk.Entry(form, textvariable=self.password_var, show="*", width=48).grid(row=2, column=1, sticky="ew", padx=(12, 0), pady=4)
        ttk.Label(
            form,
            text="Address through the port, for example http://213.59.112.27:5000. The sign-in stays on this PC.",
            style="Hint.TLabel",
            wraplength=560,
        ).grid(row=3, column=1, sticky="w", padx=(12, 0), pady=(2, 8))
        actions = ttk.Frame(meetings)
        actions.pack(anchor="w")
        self._signin_button = ttk.Button(actions, text="Sign in", command=self.sign_in, width=14)
        self._signin_button.pack(side="left")
        self._signout_button = ttk.Button(actions, text="Sign out", command=self.sign_out, width=14)
        self._signout_button.pack(side="left", padx=(8, 0))
        ttk.Label(meetings, textvariable=self.account_status, style="Status.TLabel").pack(anchor="w", pady=(8, 0))
        ttk.Label(meetings, textvariable=self.meeting_status, style="Status.TLabel").pack(anchor="w", pady=(2, 0))

    def run(self) -> None:
        import pystray

        menu = pystray.Menu(
            pystray.MenuItem("Show App", self.show_window, default=True),
            pystray.MenuItem("Sign in", self.show_sign_in),
            pystray.MenuItem("Help", self.show_help),
            pystray.MenuItem("Exit", self.exit_app),
        )
        self.icon = pystray.Icon("AutoKey", self._icon_image, "AutoKey", menu)
        self._register_hotkeys()
        self._sync_config_from_server()
        self.icon.run_detached(self._on_icon_ready)
        logging.info("AutoKey %s ready with %s hotkey(s)", APP_VERSION, len(self._bindings))
        tracker = self._tracker_settings()
        if tracker.get("email") and tracker.get("password"):
            logging.info("Checking Job Tracker for interviews in the next hour")
        else:
            logging.info("Interview reminders are off until you sign in")
        self.root.mainloop()

    def _on_icon_ready(self, icon) -> None:
        icon.visible = True
        try:
            icon.notify("Right-click the tray icon and choose Show App.", "AutoKey is running")
        except Exception:
            logging.exception("Could not show the startup notification")

    def show_sign_in(self, _icon=None, _item=None) -> None:
        self.root.after(0, self._show_sign_in)

    def _show_sign_in(self) -> None:
        self._show_window()
        self.notebook.select(self._meetings_tab)

    def show_help(self, _icon=None, _item=None) -> None:
        message_box(f"Version {APP_VERSION}", "AutoKey")

    def show_window(self, _icon=None, _item=None) -> None:
        self.root.after(0, self._show_window)

    def _show_window(self) -> None:
        self._sync_startup_checkbox()
        self._refresh_rows()
        self._refresh_pairs()
        self.root.attributes("-alpha", 1.0)
        self.root.deiconify()
        self.root.state("normal")
        self.root.lift()
        self.root.attributes("-topmost", True)
        self.root.after(200, lambda: self.root.attributes("-topmost", False))
        self.root.focus_force()

    def hide_window(self) -> None:
        self.root.withdraw()

    def _sync_startup_checkbox(self) -> None:
        self.start_with_windows.set(is_startup_enabled())

    def _on_startup_toggle(self) -> None:
        wanted = bool(self.start_with_windows.get())
        try:
            set_startup_enabled(wanted)
        except Exception:
            logging.exception("Could not update the Windows startup setting")
            self._sync_startup_checkbox()
            messagebox.showerror(
                "AutoKey",
                "Could not change the Windows startup setting.",
                parent=self.root,
            )
            return
        self._sync_startup_checkbox()
        if wanted and is_startup_enabled():
            self.status.set("AutoKey will start when you sign in to Windows.")
        elif not wanted and not is_startup_enabled():
            self.status.set("AutoKey will not start with Windows.")

    def _transform_settings(self) -> tuple[bool, list[tuple[str, str]]]:
        with self._pair_lock:
            return self._transform_on, list(self._pairs)

    def _on_transform_toggle(self) -> None:
        self._transform_on = bool(self.transform_enabled.get())
        self._save()
        if self._transform_on:
            self.status.set("Clipboard transformation is on.")
        else:
            self.status.set("Clipboard transformation is off.")

    def _on_clipboard_transformed(self) -> None:
        self._set_status("Updated the clipboard using your spell pairs.")

    def _tracker_settings(self) -> dict:
        with self._tracker_lock:
            return dict(self._tracker)

    def sign_in(self) -> None:
        server = server_origin(self.server_var.get())
        email = self.email_var.get().strip()
        password = self.password_var.get()
        if not server:
            messagebox.showerror(
                "AutoKey",
                "Enter the Job Tracker server address, including the port.",
                parent=self.root,
            )
            return
        if not email or not password:
            messagebox.showerror("AutoKey", "Enter the email and the password.", parent=self.root)
            return
        self.server_var.set(server)
        self.account_status.set("Signing in...")
        self._signin_button.state(["disabled"])

        def work() -> None:
            client = JobTrackerClient(server, email, password)
            try:
                client.login()
                payload = client._request("GET", "/auth/me")
                user = payload.get("user") if isinstance(payload, dict) else {}
                name = str((user or {}).get("name") or email).strip() or email
            except TrackerError as exc:
                message = str(exc)
                self.root.after(0, lambda: self._sign_in_failed(message))
                return
            except Exception:
                logging.exception("Sign-in failed")
                self.root.after(0, lambda: self._sign_in_failed("Could not reach Job Tracker."))
                return
            token = client.token
            self.root.after(0, lambda: self._sign_in_ok(server, email, password, token, name))

        threading.Thread(target=work, name="autokey-signin", daemon=True).start()

    def _sign_in_failed(self, message: str) -> None:
        try:
            self._signin_button.state(["!disabled"])
        except tk.TclError:
            return
        self._refresh_account_status()
        self.meeting_status.set(message)
        messagebox.showerror("AutoKey", message, parent=self.root)

    def _sign_in_ok(self, server: str, email: str, password: str, token: str, name: str) -> None:
        try:
            self._signin_button.state(["!disabled"])
        except tk.TclError:
            return
        with self._tracker_lock:
            self._tracker = {
                "server": server,
                "email": email,
                "password": password,
                "token": token,
                "name": name,
            }
        self._write_local()
        self._reminders.use_token(token)
        self._refresh_account_status()
        self.meeting_status.set("Checking interviews...")
        logging.info("Signed in to Job Tracker as %s", email)
        self._reminders.check_soon()
        self._sync_config_from_server()

    def sign_out(self) -> None:
        with self._tracker_lock:
            server = self._tracker.get("server") or server_origin(self.server_var.get()) or DEFAULT_SERVER
            self._tracker = {"server": server, "email": "", "password": "", "token": "", "name": ""}
        self.email_var.set("")
        self.password_var.set("")
        self._save()
        self._reminders.forget()
        self._refresh_account_status()
        self.meeting_status.set("Signed out. Interview reminders are off.")

    def _refresh_account_status(self) -> None:
        with self._tracker_lock:
            name = str(self._tracker.get("name") or "")
            email = str(self._tracker.get("email") or "")
            token = str(self._tracker.get("token") or "")
        if token or email:
            self.account_status.set(f"Signed in as {name or email}.")
        else:
            self.account_status.set("Not signed in.")

    def _on_tracker_token(self, token: str) -> None:
        with self._tracker_lock:
            if self._tracker.get("token") == token:
                return
            self._tracker = {**self._tracker, "token": token}
        self._save()

    def _on_meeting_status(self, text: str) -> None:
        def apply() -> None:
            try:
                self.meeting_status.set(text)
            except tk.TclError:
                pass

        try:
            self.root.after(0, apply)
        except tk.TclError:
            logging.exception("Could not update the meeting status")

    def _on_meetings_due(self, meetings: list[dict], now: datetime, acknowledge=None) -> None:
        logging.info("Showing a reminder for %s interview(s)", len(meetings))
        text = "\n\n".join(meeting_alert_text(meeting, now) for meeting in meetings)
        ids = [str(meeting.get("interviewId")) for meeting in meetings if meeting.get("interviewId")]

        def show() -> None:
            try:
                self._show_alert(text)
            except tk.TclError:
                logging.exception("Could not show the meeting reminder")
                return
            if acknowledge and ids:
                acknowledge(ids)

        try:
            self.root.after(0, show)
        except tk.TclError:
            logging.exception("Could not schedule the meeting reminder")

    def _show_alert(self, text: str) -> None:
        if self._alert is not None:
            try:
                if self._alert.winfo_exists():
                    self._alert.refresh(text)
                    return
            except tk.TclError:
                pass
        self._alert = MeetingAlert(self.root, text)

    def add_pair(self) -> None:
        self._edit_pair(None)

    def edit_pair(self) -> None:
        selected = self.pair_tree.selection()
        if not selected:
            messagebox.showinfo("AutoKey", "Select a spell pair first.", parent=self.root)
            return
        index = int(selected[0].split(":", 1)[1])
        if index < 0 or index >= len(self._pairs):
            return
        self._edit_pair(index)

    def remove_pair(self) -> None:
        selected = self.pair_tree.selection()
        if not selected:
            messagebox.showinfo("AutoKey", "Select a spell pair first.", parent=self.root)
            return
        index = int(selected[0].split(":", 1)[1])
        if index < 0 or index >= len(self._pairs):
            return
        source, dest = self._pairs[index]
        label = f"{source} → {dest}" if dest else f"{source} → (empty)"
        if not messagebox.askyesno("AutoKey", f"Remove {label}?", parent=self.root):
            return
        with self._pair_lock:
            self._pairs = [item for i, item in enumerate(self._pairs) if i != index]
        self._save()
        self._refresh_pairs()

    def _edit_pair(self, index: int | None) -> None:
        current = self._pairs[index] if index is not None else None
        taken = {source for i, (source, _dest) in enumerate(self._pairs) if index is None or i != index}
        dialog = PairDialog(self.root, current, taken)
        self.root.wait_window(dialog)
        if dialog.result is None:
            return
        source, dest = dialog.result
        with self._pair_lock:
            if index is None:
                self._pairs.append((source, dest))
            else:
                self._pairs[index] = (source, dest)
        self._save()
        self._refresh_pairs()

    def _refresh_pairs(self) -> None:
        for iid in self.pair_tree.get_children():
            self.pair_tree.delete(iid)
        with self._pair_lock:
            rows = list(self._pairs)
        for index, (source, dest) in enumerate(rows):
            self.pair_tree.insert("", "end", iid=f"pair:{index}", values=(source, dest if dest else "(empty)"))
        self.transform_enabled.set(self._transform_on)

    def exit_app(self, _icon=None, _item=None) -> None:
        if self._quitting:
            return
        self._quitting = True
        self._queue.put(None)
        try:
            self.root.after(0, self._shutdown)
        except tk.TclError:
            logging.exception("Could not schedule shutdown")

    def _shutdown(self) -> None:
        self._reminders.stop()
        self._watcher.stop()
        self._worker.join(timeout=5)
        self._suspend_hotkeys()
        try:
            keyboard.unhook_all()
        except Exception:
            logging.exception("Could not remove the keyboard hook")
        if self.icon is not None:
            try:
                self.icon.stop()
            except Exception:
                logging.exception("Could not remove the tray icon")
        self.root.destroy()

    def add_binding(self) -> None:
        self._edit(None)

    def edit_binding(self) -> None:
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("AutoKey", "Select a hotkey first.", parent=self.root)
            return
        hotkey = self._hotkey_from_row(selected[0])
        current = next((item for item in self._bindings if item[0] == hotkey), None)
        if current is None:
            return
        self._edit(current)

    def remove_binding(self) -> None:
        selected = self.tree.selection()
        if not selected:
            messagebox.showinfo("AutoKey", "Select a hotkey first.", parent=self.root)
            return
        hotkey = self._hotkey_from_row(selected[0])
        if not messagebox.askyesno("AutoKey", f"Remove {pretty_hotkey(hotkey)}?", parent=self.root):
            return
        self._bindings = [item for item in self._bindings if item[0] != hotkey]
        self._save()
        self._refresh_rows()
        self._register_hotkeys()

    def _edit(self, current: tuple[str, str] | None) -> None:
        self._suspend_hotkeys()
        try:
            taken = {hotkey for hotkey, _path in self._bindings if current is None or hotkey != current[0]}
            dialog = BindingDialog(self.root, current, taken)
            self.root.wait_window(dialog)
            if dialog.result is None:
                return
            hotkey, path = dialog.result
            if current is not None:
                self._bindings = [item for item in self._bindings if item[0] != current[0]]
            self._bindings.append((hotkey, path))
            self._bindings.sort(key=lambda item: item[0])
            self._save()
            self._refresh_rows()
        finally:
            self._register_hotkeys()

    def _row_id(self, hotkey: str) -> str:
        return "hk:" + hotkey

    def _hotkey_from_row(self, row_id: str) -> str:
        return row_id[3:]

    def _read_config_lists(self, data: dict) -> tuple[list[tuple[str, str]], bool, list[tuple[str, str]]]:
        bindings = []
        seen: set[str] = set()
        for item in data.get("bindings") or []:
            if not isinstance(item, dict):
                continue
            hotkey = str(item.get("hotkey") or "").strip()
            path = str(item.get("file") or "").strip()
            if not hotkey or not path or hotkey in seen:
                continue
            seen.add(hotkey)
            bindings.append((hotkey, path))
        transform = data.get("clipboard_transform") or data.get("clipboardTransform") or {}
        enabled = bool(transform.get("enabled", False))
        pairs: list[tuple[str, str]] = []
        seen_spells: set[str] = set()
        for item in transform.get("pairs") or []:
            if not isinstance(item, dict):
                continue
            source = str(item.get("from", ""))
            dest = str(item.get("to", ""))
            if not source or source in seen_spells:
                continue
            seen_spells.add(source)
            pairs.append((source, dest))
        return bindings, enabled, pairs

    def _apply_config(self, data: dict) -> None:
        bindings, enabled, pairs = self._read_config_lists(data)
        self._bindings = bindings
        with self._pair_lock:
            self._pairs = pairs
            self._transform_on = enabled
        self.transform_enabled.set(enabled)

    def _config_payload(self) -> dict:
        with self._pair_lock:
            pairs = list(self._pairs)
            enabled = self._transform_on
        return {
            "bindings": [{"hotkey": hotkey, "file": path} for hotkey, path in self._bindings],
            "clipboard_transform": {
                "enabled": enabled,
                "pairs": [{"from": source, "to": dest} for source, dest in pairs],
            },
        }

    def _tracker_client(self) -> JobTrackerClient | None:
        settings = self._tracker_settings()
        if not settings.get("email") or not settings.get("password"):
            return None
        return JobTrackerClient(
            settings.get("server") or "",
            settings.get("email") or "",
            settings.get("password") or "",
            settings.get("token") or "",
        )

    def _load(self) -> None:
        if not CONFIG_PATH.exists():
            return
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            self._apply_config(data)
            tracker = data.get("job_tracker") or {}
            server = server_origin(str(tracker.get("server") or "")) or DEFAULT_SERVER
            email = str(tracker.get("email") or "")
            password = str(tracker.get("password") or "")
            token = str(tracker.get("token") or "")
            name = str(tracker.get("name") or "")
            with self._tracker_lock:
                self._tracker = {
                    "server": server,
                    "email": email,
                    "password": password,
                    "token": token,
                    "name": name,
                }
            self.server_var.set(server)
            self.email_var.set(email)
            self.password_var.set(password)
            self._refresh_account_status()
        except Exception:
            logging.exception("Could not read %s", CONFIG_PATH)
            backup = CONFIG_PATH.with_suffix(".json.bak")
            try:
                CONFIG_PATH.replace(backup)
            except OSError:
                pass
            messagebox.showwarning("AutoKey", "The saved hotkey list could not be read. Starting with an empty list.")

    def _write_local(self) -> None:
        with self._tracker_lock:
            tracker = dict(self._tracker)
        payload = self._config_payload()
        payload["job_tracker"] = {
            "server": tracker.get("server") or DEFAULT_SERVER,
            "email": tracker.get("email") or "",
            "password": tracker.get("password") or "",
            "token": tracker.get("token") or "",
            "name": tracker.get("name") or "",
        }
        temporary = CONFIG_PATH.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(CONFIG_PATH)

    def _save(self) -> None:
        self._write_local()
        payload = self._config_payload()
        settings = self._tracker_settings()
        self._push_config(settings, payload)

    def _push_config(self, settings: dict, payload: dict) -> None:
        if not settings.get("email") or not settings.get("password"):
            return

        def work() -> None:
            client = JobTrackerClient(
                settings.get("server") or "",
                settings.get("email") or "",
                settings.get("password") or "",
                settings.get("token") or "",
            )
            try:
                client.save_config(payload)
            except Exception:
                logging.exception("Could not store AutoKey configuration")
                try:
                    self.root.after(0, lambda: self.status.set("Could not store the configuration on the server."))
                except tk.TclError:
                    pass
                return
            if client.token and client.token != (settings.get("token") or ""):
                self._on_tracker_token(client.token)

        threading.Thread(target=work, name="autokey-config", daemon=True).start()

    def _sync_config_from_server(self) -> None:
        client = self._tracker_client()
        if client is None:
            return
        local_payload = self._config_payload()

        def work() -> None:
            try:
                remote = client.fetch_config()
            except Exception:
                logging.exception("Could not load AutoKey configuration")
                try:
                    self.root.after(0, lambda: self.status.set("Could not load the configuration from the server."))
                except tk.TclError:
                    pass
                return
            if client.token:
                self._reminders.use_token(client.token)

            def apply() -> None:
                if not remote.get("configured"):
                    has_local = bool(local_payload["bindings"] or local_payload["clipboard_transform"]["pairs"] or local_payload["clipboard_transform"]["enabled"])
                    if has_local:
                        self._push_config(self._tracker_settings(), local_payload)
                        self.status.set("Stored the current configuration on the server.")
                    return
                self._apply_config(remote)
                self._write_local()
                self._refresh_rows()
                self._refresh_pairs()
                self._register_hotkeys()
                self.status.set("Loaded hotkeys and clipboard settings from the server.")

            try:
                self.root.after(0, apply)
            except tk.TclError:
                logging.exception("Could not apply the server configuration")

        threading.Thread(target=work, name="autokey-config-sync", daemon=True).start()

    def export_configuration(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="Export AutoKey configuration",
            defaultextension=".json",
            initialfile="autokey-config.json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        client = self._tracker_client()
        fallback = self._config_payload()

        def work() -> None:
            payload = fallback
            if client is not None:
                try:
                    remote = client.fetch_config()
                    if remote.get("configured"):
                        payload = {
                            "bindings": remote.get("bindings") or [],
                            "clipboard_transform": remote.get("clipboard_transform") or {"enabled": False, "pairs": []},
                        }
                except Exception:
                    logging.exception("Could not export the server configuration")
            try:
                Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
            except OSError:
                logging.exception("Could not write the export file")
                self.root.after(0, lambda: messagebox.showerror("AutoKey", "Could not write that file.", parent=self.root))
                return
            self.root.after(0, lambda: self.status.set("Exported the configuration."))

        threading.Thread(target=work, name="autokey-export", daemon=True).start()

    def import_configuration(self) -> None:
        if self._tracker_client() is None:
            messagebox.showerror(
                "AutoKey",
                "Sign in before importing so the configuration is stored on the server.",
                parent=self.root,
            )
            return
        path = filedialog.askopenfilename(
            parent=self.root,
            title="Import AutoKey configuration",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            messagebox.showerror("AutoKey", "That file could not be read.", parent=self.root)
            return
        if not isinstance(data, dict) or not isinstance(data.get("bindings"), list):
            messagebox.showerror("AutoKey", "That file is not an AutoKey configuration.", parent=self.root)
            return
        client = self._tracker_client()

        def work() -> None:
            try:
                saved = client.import_config(
                    {
                        "bindings": data.get("bindings") or [],
                        "clipboard_transform": data.get("clipboard_transform") or data.get("clipboardTransform") or {},
                    }
                )
            except TrackerError as exc:
                message = str(exc)
                self.root.after(0, lambda: messagebox.showerror("AutoKey", message, parent=self.root))
                return
            except Exception:
                logging.exception("Could not import AutoKey configuration")
                self.root.after(
                    0,
                    lambda: messagebox.showerror("AutoKey", "Could not store the configuration on the server.", parent=self.root),
                )
                return

            def apply() -> None:
                self._apply_config(saved)
                self._write_local()
                self._refresh_rows()
                self._refresh_pairs()
                self._register_hotkeys()
                self.status.set("Imported the configuration and stored it on the server.")

            self.root.after(0, apply)

        threading.Thread(target=work, name="autokey-import", daemon=True).start()

    def _refresh_rows(self) -> None:
        current = set(self.tree.get_children())
        wanted = {self._row_id(hotkey) for hotkey, _path in self._bindings}
        for row_id in current - wanted:
            self.tree.delete(row_id)
        for hotkey, path in self._bindings:
            row_id = self._row_id(hotkey)
            values = (pretty_hotkey(hotkey), path, file_preview(path))
            if self.tree.exists(row_id):
                self.tree.item(row_id, values=values)
            else:
                self.tree.insert("", "end", iid=row_id, values=values)
        count = len(self._bindings)
        if self.status.get().startswith("Pasted") or self.status.get().startswith("Paste"):
            return
        if count == 0:
            self.status.set("No hotkeys yet. Add one, then hide this window and try it.")
        elif count == 1:
            self.status.set("1 hotkey active.")
        else:
            self.status.set(f"{count} hotkeys active.")

    def _register_hotkeys(self) -> None:
        failed = self._router.set_bindings(self._bindings)
        if failed:
            self.status.set("Could not register: " + ", ".join(failed))
        else:
            self._refresh_rows()

    def _suspend_hotkeys(self) -> None:
        self._router.stop()

    def _enqueue(self, path: str) -> None:
        if not self._quitting:
            self._queue.put(path)

    def _paste_loop(self) -> None:
        while True:
            path = self._queue.get()
            try:
                if path is None:
                    return
                self._paste_file(path)
            except Exception as exc:
                logging.exception("Paste failed")
                message = str(exc) or "Could not paste that text."
                self._set_status(message)
                self._notify(message)
            finally:
                self._queue.task_done()

    def _paste_file(self, path: str) -> None:
        try:
            text = read_text_file(path)
        except Exception:
            logging.exception("Could not read %s", path)
            self._set_status(f"Could not read {path}")
            self._notify(f"Could not read {Path(path).name}")
            return
        paste_text(text)
        logging.info("Pasted %s (%s characters) and restored the clipboard", path, len(text))
        self._set_status(f"Pasted {Path(path).name}. Clipboard restored.")

    def _set_status(self, text: str) -> None:
        if self._quitting:
            return
        try:
            self.root.after(0, lambda t=text: self.status.set(t))
        except (tk.TclError, RuntimeError):
            try:
                self.status.set(text)
            except Exception:
                pass

    def _notify(self, message: str) -> None:
        if self.icon is None:
            return
        try:
            self.icon.notify(message, "AutoKey")
        except Exception:
            logging.exception("Notification failed")


def self_test() -> None:
    original = backup_clipboard()
    if original is None:
        raise SystemExit("clipboard busy")
    try:
        set_clipboard_text("autokey original ✓")
        saved = backup_clipboard()
        set_clipboard_text("autokey pasted 你好")
        if get_clipboard_text() != "autokey pasted 你好":
            raise SystemExit("clipboard write failed")
        if not restore_clipboard(saved):
            raise SystemExit("clipboard restore failed")
        if get_clipboard_text() != "autokey original ✓":
            raise SystemExit(f"restored text mismatch: {get_clipboard_text()!r}")
        sample = APP_DIR / "_selftest_snippet.txt"
        sample.write_bytes("line one\r\nline two\n".encode("utf-8"))
        try:
            if read_text_file(str(sample)) != "line one\r\nline two\n":
                raise SystemExit("text read failed")
        finally:
            sample.unlink(missing_ok=True)
        staged = {}

        def fake_release() -> None:
            staged["released"] = True

        def fake_send(combo: str) -> None:
            if get_clipboard_text() != "snippet body":
                raise SystemExit("paste staged the wrong clipboard text")
            staged["sent"] = combo

        global release_modifiers
        real_send = keyboard.send
        real_release = release_modifiers
        try:
            keyboard.send = fake_send
            release_modifiers = fake_release
            paste_text("snippet body")
        finally:
            keyboard.send = real_send
            release_modifiers = real_release
        if staged.get("sent") != "ctrl+v" or not staged.get("released"):
            raise SystemExit("paste did not send ctrl+v")
        if get_clipboard_text() != "autokey original ✓":
            raise SystemExit("clipboard was not restored after paste")
        if split_hotkey("ctrl+shift+a") != (frozenset({"ctrl", "shift"}), "a"):
            raise SystemExit("split failed")
        if hotkey_matches("ctrl+shift+a", frozenset({"ctrl"}), "a"):
            raise SystemExit("ctrl+a must not match ctrl+shift+a")
        if hotkey_matches("ctrl+shift+a", frozenset({"ctrl", "shift", "alt"}), "a"):
            raise SystemExit("extra modifiers must not match")
        if not hotkey_matches("ctrl+shift+a", frozenset({"ctrl", "shift"}), "A"):
            raise SystemExit("ctrl+shift+a should match")
        if hotkey_matches("ctrl+a", frozenset({"ctrl", "shift"}), "a"):
            raise SystemExit("ctrl+shift+a must not match ctrl+a")
        if not hotkey_matches("ctrl+a", frozenset({"ctrl"}), "a"):
            raise SystemExit("ctrl+a should match")
        if canonical_hotkey(["left ctrl", "left alt", "1"]) != "ctrl+alt+1":
            raise SystemExit("hotkey name failed")
        if normalize_hotkey("Ctrl + Alt + A") != "ctrl+alt+a":
            raise SystemExit("normalize failed")
        if is_bindable("ctrl+shift") or not is_bindable("ctrl+shift+a"):
            raise SystemExit("bindable check failed")
        keyboard.parse_hotkey("ctrl+alt+1")
        if transform_text("A and C stay", [("A", "B"), ("C", "B")]) != "B and B stay":
            raise SystemExit("transform pair failed")
        if transform_text("AA", [("A", "B"), ("AA", "Z")]) != "Z":
            raise SystemExit("longest spell should win")
        if transform_text("A", [("A", "B"), ("B", "C")]) != "B":
            raise SystemExit("replacements must not chain")
        if transform_text("hello", [("A", "B")]) != "hello":
            raise SystemExit("unrelated text should stay")
        make_icon()
        if interview_step_label(1) != "1st interview" or interview_step_label(2) != "2nd interview":
            raise SystemExit("step label failed")
        if interview_step_label(3) != "3rd interview" or interview_step_label(11) != "11th interview":
            raise SystemExit("step label failed")
        if interview_step_label(21) != "21st interview":
            raise SystemExit("step label failed")
        if api_root("http://213.59.112.27:5000/api/jobs") != "http://213.59.112.27:5000/api":
            raise SystemExit("api root failed")
        if server_origin("213.59.112.27:5000") != "http://213.59.112.27:5000":
            raise SystemExit("server origin failed")
        now = datetime(2026, 11, 12, 15, 0, tzinfo=timezone.utc)

        def sample(minutes: int, ident: str, step: int = 2) -> dict:
            start = (now + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")
            return {
                "jobId": "j1",
                "jobTitle": "AI Engineer",
                "companyName": "Stride",
                "step": step,
                "interview": {
                    "_id": ident,
                    "startsAt": start,
                    "interviewers": [{"name": "Avery Chen"}],
                },
            }

        remembered: dict[str, datetime] = {}
        if len(due_reminders([sample(50, "hour")], remembered, now)) != 1:
            raise SystemExit("a meeting inside the hour should remind")
        remembered["hour"] = now
        if due_reminders([sample(50, "hour")], remembered, now + timedelta(minutes=10)):
            raise SystemExit("the 15 minute reminder came early")
        if len(due_reminders([sample(50, "hour")], remembered, now + timedelta(minutes=15))) != 1:
            raise SystemExit("the 15 minute reminder was missed")
        soon_seen = {"soon": now}
        if due_reminders([sample(25, "soon")], soon_seen, now + timedelta(minutes=9)):
            raise SystemExit("the 10 minute reminder came early")
        if len(due_reminders([sample(25, "soon")], soon_seen, now + timedelta(minutes=10))) != 1:
            raise SystemExit("the 10 minute reminder was missed")
        close_seen = {"close": now}
        if due_reminders([sample(12, "close")], close_seen, now + timedelta(minutes=4)):
            raise SystemExit("the 5 minute reminder came early")
        if len(due_reminders([sample(12, "close")], close_seen, now + timedelta(minutes=5))) != 1:
            raise SystemExit("the 5 minute reminder was missed")
        if due_reminders([sample(90, "later")], {}, now):
            raise SystemExit("a meeting beyond an hour should stay quiet")
        if due_reminders([sample(-5, "past")], {}, now):
            raise SystemExit("a past meeting should stay quiet")
        date_only = {
            "companyName": "Stride",
            "step": 1,
            "interview": {"_id": "day", "date": now.isoformat(), "interviewers": [{"name": "Avery Chen"}]},
        }
        if due_reminders([date_only], {}, now):
            raise SystemExit("a date-only interview should stay quiet")
        alert = meeting_alert_text(sample(40, "text"), now)
        if "Stride" not in alert or "2nd interview" not in alert or "Avery Chen" not in alert:
            raise SystemExit("alert text is missing the meeting details")
        if "Remaining: 40 minutes" not in alert:
            raise SystemExit("alert text is missing the remaining time")
        command = startup_command()
        if str(APP_DIR) not in command or "autokey" not in command.casefold():
            raise SystemExit("startup command is wrong")
        if not is_our_startup_command(command) or is_our_startup_command(r'"C:\Other\autokey.py"'):
            raise SystemExit("startup path check failed")
        test_name = "AutoKey.SelfTest"
        previous = read_startup_command(test_name)
        try:
            set_startup_enabled(True, value_name=test_name)
            if not is_startup_enabled(value_name=test_name):
                raise SystemExit("startup enable failed")
            if read_startup_command(test_name) != command:
                raise SystemExit("startup command was not stored")
            set_startup_enabled(False, value_name=test_name)
            if is_startup_enabled(value_name=test_name) or read_startup_command(test_name) is not None:
                raise SystemExit("startup disable failed")
        finally:
            if previous is None:
                try:
                    set_startup_enabled(False, value_name=test_name)
                except OSError:
                    pass
            else:
                with winreg.CreateKey(winreg.HKEY_CURRENT_USER, STARTUP_RUN_KEY) as key:
                    winreg.SetValueEx(key, test_name, 0, winreg.REG_SZ, previous)
        print("self-test ok")
    finally:
        restore_clipboard(original)


def hide_console() -> None:
    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if hwnd:
        ctypes.windll.user32.ShowWindow(hwnd, 0)


def main() -> None:
    if sys.platform != "win32":
        raise SystemExit("AutoKey runs on Windows.")
    hide_console()
    enable_dpi()
    setup_logging()
    if "--self-test" in sys.argv:
        self_test()
        return
    if not acquire_single_instance():
        message_box(
            "AutoKey is already running.\nRight-click the tray icon and choose Show App.",
            "AutoKey",
        )
        return
    try:
        App().run()
    except Exception:
        logging.exception("AutoKey stopped")
        message_box("AutoKey hit an error. Details are in autokey.log next to the script.", "AutoKey", error=True)


if __name__ == "__main__":
    main()
