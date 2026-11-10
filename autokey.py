"""AutoKey: paste a linked text file with a global hotkey.

The window starts hidden. Right-click the tray icon and choose Show App.
Hide Window (or the window's close button) hides it again without quitting.
Exit is only on the tray menu.

A hotkey copies that file's text into the focused box, then puts back whatever
was on the clipboard before the paste.

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
import winreg
from ctypes import wintypes
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

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "bindings.json"
LOG_PATH = APP_DIR / "autokey.log"
STARTUP_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
STARTUP_VALUE_NAME = "AutoKey"

PASTE_SETTLE_SECONDS = 0.30
CLIPBOARD_POLL_SECONDS = 0.12
CLIPBOARD_RETRIES = 25
MAX_FORMAT_BYTES = 64 * 1024 * 1024

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
        self._apply_style()
        self._build()
        self._load()
        self._sync_startup_checkbox()
        self._refresh_rows()
        self._refresh_pairs()
        self.root.protocol("WM_DELETE_WINDOW", self.hide_window)
        self._worker.start()
        self._watcher.start()

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
        ).pack(anchor="w", pady=(0, 12))

        footer = ttk.Frame(outer)
        footer.pack(side="bottom", fill="x", pady=(12, 0))
        ttk.Button(footer, text="Hide Window", command=self.hide_window, width=16).pack(side="right")
        tk.Checkbutton(
            footer,
            text="Start AutoKey when Windows starts",
            variable=self.start_with_windows,
            command=self._on_startup_toggle,
            font=("Segoe UI", 10),
            anchor="w",
        ).pack(anchor="w")
        ttk.Label(footer, textvariable=self.status, style="Status.TLabel").pack(anchor="w", pady=(4, 0))

        notebook = ttk.Notebook(outer)
        notebook.pack(fill="both", expand=True)

        hotkeys = ttk.Frame(notebook, padding=8)
        notebook.add(hotkeys, text="Hotkeys")
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

        transform = ttk.Frame(notebook, padding=8)
        notebook.add(transform, text="Clipboard transform")
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

    def run(self) -> None:
        import pystray

        menu = pystray.Menu(
            pystray.MenuItem("Show App", self.show_window, default=True),
            pystray.MenuItem("Exit", self.exit_app),
        )
        self.icon = pystray.Icon("AutoKey", self._icon_image, "AutoKey", menu)
        self._register_hotkeys()
        self.icon.run_detached(self._on_icon_ready)
        logging.info("AutoKey ready with %s hotkey(s)", len(self._bindings))
        self.root.mainloop()

    def _on_icon_ready(self, icon) -> None:
        icon.visible = True
        try:
            icon.notify("Right-click the tray icon and choose Show App.", "AutoKey is running")
        except Exception:
            logging.exception("Could not show the startup notification")

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

    def _load(self) -> None:
        if not CONFIG_PATH.exists():
            return
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            raw = data.get("bindings", [])
            bindings = []
            seen: set[str] = set()
            for item in raw:
                hotkey = str(item["hotkey"])
                path = str(item["file"])
                if hotkey in seen:
                    continue
                seen.add(hotkey)
                bindings.append((hotkey, path))
            self._bindings = bindings
            transform = data.get("clipboard_transform") or {}
            enabled = bool(transform.get("enabled", False))
            pairs: list[tuple[str, str]] = []
            seen_spells: set[str] = set()
            for item in transform.get("pairs") or []:
                source = str(item.get("from", ""))
                dest = str(item.get("to", ""))
                if not source or source in seen_spells:
                    continue
                seen_spells.add(source)
                pairs.append((source, dest))
            with self._pair_lock:
                self._pairs = pairs
                self._transform_on = enabled
            self.transform_enabled.set(enabled)
        except Exception:
            logging.exception("Could not read %s", CONFIG_PATH)
            backup = CONFIG_PATH.with_suffix(".json.bak")
            try:
                CONFIG_PATH.replace(backup)
            except OSError:
                pass
            messagebox.showwarning("AutoKey", "The saved hotkey list could not be read. Starting with an empty list.")

    def _save(self) -> None:
        with self._pair_lock:
            pairs = list(self._pairs)
            enabled = self._transform_on
        payload = {
            "bindings": [{"hotkey": hotkey, "file": path} for hotkey, path in self._bindings],
            "clipboard_transform": {
                "enabled": enabled,
                "pairs": [{"from": source, "to": dest} for source, dest in pairs],
            },
        }
        temporary = CONFIG_PATH.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        temporary.replace(CONFIG_PATH)

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
