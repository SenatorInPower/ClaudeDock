# -*- coding: utf-8 -*-
"""Inject text + Enter into a LIVE Claude Code terminal (as if typed by hand).

`claude agents --json` gives each interactive session a `pid`. The session's
window (PowerShell/cmd conhost, or a VS Code integrated terminal running on a
ConPTY) shares one console object with that pid. We attach to that console and
push the text as console input records, then a carriage return — the running
`claude` REPL reads it from stdin and continues IN THAT SAME WINDOW (context
preserved), exactly like the user typed it and pressed Enter.

This is fundamentally different from `claude -p --resume`, which spawns a SECOND
headless process. Injection keeps the work in the open window the user is watching.

AttachConsole() is process-global and, once a long-lived process has detached
from its original console, every later AttachConsole in it fails — so the attach
is done in a short-lived DETACHED helper process (see send_to_terminal). Each
injection gets a clean process; the dock and webapp consoles are never touched.

Two hard-won facts about delivering to the REAL claude:
1. claude ships as claude.exe (a Bun-compiled binary). Unlike node/libuv it DROPS
   console input records whose virtual-key/scan-code are 0. Fix: send every
   printable char as a VK_PACKET («inject arbitrary Unicode» key) with a non-zero
   scan code — works for ASCII, Cyrillic and mixed text. See _vk_sc().
2. The records ONLY reach claude.exe when written from the LONG-LIVED caller
   in-process — a freshly-spawned helper attaches to the same console and
   WriteConsoleInput reports success, but Bun never sees the records. So we attach
   IN-PROCESS. AttachConsole is process-global and the once-only-per-detach quirk
   bites if we FreeConsole after each write, so we DON'T: we detach only at the
   START of the next injection and otherwise stay attached to the last target.
   A console-ctrl handler keeps us alive if that target window is later closed.
Both verified end-to-end against a real claude REPL (incl. Russian).
"""
import ctypes
import ctypes.wintypes as wt
import os
import sys
import threading
from pathlib import Path

_IS_WIN = os.name == "nt"
_LOCK = threading.Lock()      # AttachConsole is process-global -> one injection at a time
_ctrl_installed = False
_ctrl_handler_ref = None      # keep a ref so the callback isn't GC'd

if _IS_WIN:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    u32 = ctypes.WinDLL("user32", use_last_error=True)

    KEY_EVENT = 0x0001
    VK_RETURN = 0x0D
    VK_PACKET = 0xE7   # «отправить произвольный Unicode» — для букв вне раскладки

    _HANDLER = ctypes.WINFUNCTYPE(wt.BOOL, wt.DWORD)

    STD_OPEN = dict(GENERIC_READ=0x80000000, GENERIC_WRITE=0x40000000,
                    SHARE_RW=0x00000003, OPEN_EXISTING=3)

    class _CHAR_UNION(ctypes.Union):
        _fields_ = [("UnicodeChar", wt.WCHAR), ("AsciiChar", ctypes.c_char)]

    class _KEY_EVENT_RECORD(ctypes.Structure):
        _fields_ = [
            ("bKeyDown", wt.BOOL),
            ("wRepeatCount", wt.WORD),
            ("wVirtualKeyCode", wt.WORD),
            ("wVirtualScanCode", wt.WORD),
            ("uChar", _CHAR_UNION),
            ("dwControlKeyState", wt.DWORD),
        ]

    class _INPUT_RECORD(ctypes.Structure):
        class _EVT(ctypes.Union):
            _fields_ = [("KeyEvent", _KEY_EVENT_RECORD)]
        _anonymous_ = ("u",)
        _fields_ = [("EventType", wt.WORD), ("u", _EVT)]


def _install_ctrl_handler():
    """Глушим события консоли (CTRL_C/CLOSE/...): мы временно прицепляемся к чужим
    консолям, и закрытие того окна не должно уронить наш долгоживущий процесс."""
    global _ctrl_installed, _ctrl_handler_ref
    if _ctrl_installed or not _IS_WIN:
        return
    def _h(evt):
        return True  # «обработано» — не передавать дальше, не завершать процесс
    _ctrl_handler_ref = _HANDLER(_h)
    k32.SetConsoleCtrlHandler(_ctrl_handler_ref, True)
    _ctrl_installed = True


def _vk_sc(ch):
    """(virtual-key, scan-code) for a char. CRUCIAL: claude.exe (Bun runtime)
    SILENTLY DROPS key records whose virtual-key/scan-code are 0 — node/libuv
    accepts them, Bun treats them as non-physical. The robust fix is VK_PACKET (the
    «inject arbitrary Unicode» key) with a non-zero scan code for EVERY printable
    char: it works for ASCII, Cyrillic and mixed text. (Mixing real per-char vkeys
    with VK_PACKET in one stream breaks delivery — keep it uniform.) Enter stays a
    real VK_RETURN so the REPL submits. Verified end-to-end against claude.exe."""
    if ch == "\r":
        return VK_RETURN, (u32.MapVirtualKeyW(VK_RETURN, 0) or 0x1C)
    return VK_PACKET, 0x01


def _records_for_text(text):
    """One key-down + key-up record per character (Enter as '\\r')."""
    recs = []
    for ch in text:
        vk, sc = _vk_sc(ch)
        for down in (1, 0):
            r = _INPUT_RECORD()
            r.EventType = KEY_EVENT
            ke = r.KeyEvent
            ke.bKeyDown = down
            ke.wRepeatCount = 1
            ke.wVirtualKeyCode = vk
            ke.wVirtualScanCode = sc
            ke.uChar.UnicodeChar = ch
            ke.dwControlKeyState = 0
            recs.append(r)
    return recs


def _attach(pid):
    """AttachConsole(pid) с одной повторной попыткой. Перед attach detach'имся от
    предыдущей консоли. Возврат (ok, errcode). Один разовый ERROR_INVALID_HANDLE
    бывает при первом уходе из «настоящей» консоли (например, у webapp) — ретраим."""
    k32.FreeConsole()                       # отцепиться от прошлой цели/своей консоли
    if k32.AttachConsole(int(pid)):
        return True, 0
    err = ctypes.get_last_error()
    k32.FreeConsole()
    if k32.AttachConsole(int(pid)):
        return True, 0
    return False, ctypes.get_last_error() or err


def _inject_attached(pid, text):
    """Attach to pid's console and write the text as console input.

    NB: intentionally does NOT FreeConsole afterwards — detaching after every write
    breaks the next AttachConsole in a long-lived process. We stay attached to the
    last target; the next call detaches at its start (see _attach). The ctrl handler
    keeps us alive if that window closes. Returns (ok, message)."""
    if not _IS_WIN:
        return False, "только Windows"
    _install_ctrl_handler()
    ok, err = _attach(pid)
    if not ok:
        return False, "AttachConsole(%s) не удался: код %d (окно закрыто?)" % (pid, err)
    k32.CreateFileW.restype = wt.HANDLE
    k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, wt.LPVOID,
                                wt.DWORD, wt.DWORD, wt.HANDLE]
    h = k32.CreateFileW("CONIN$",
                        STD_OPEN["GENERIC_READ"] | STD_OPEN["GENERIC_WRITE"],
                        STD_OPEN["SHARE_RW"], None, STD_OPEN["OPEN_EXISTING"],
                        0, None)
    if not h or h == wt.HANDLE(-1).value:
        return False, "CONIN$ открыть не удалось: код %d" % ctypes.get_last_error()
    try:
        recs = _records_for_text(text)
        n = len(recs)
        arr = (_INPUT_RECORD * n)(*recs)
        written = wt.DWORD(0)
        ok = k32.WriteConsoleInputW(h, arr, n, ctypes.byref(written))
        err = ctypes.get_last_error()
    finally:
        k32.CloseHandle(h)
    if not ok:
        return False, "WriteConsoleInput не удался: код %d" % err
    return True, "впечатано %d символов" % (written.value // 2)


def send_to_terminal(pid, text):
    """Type `text` + Enter into the live session window owned by `pid`.

    Injection is done IN-PROCESS (a freshly-spawned helper's records never reach
    claude.exe — see module docstring), serialized by a lock since AttachConsole is
    process-global. Safe to call from a worker thread. Returns (ok, message).
    """
    if not _IS_WIN:
        return False, "инъекция в терминал только на Windows"
    if not pid:
        return False, "нет PID живой сессии"
    text = (text or "").rstrip("\r\n")
    if not text:
        return False, "пустой текст"
    if not text.endswith("\r"):
        text += "\r"
    with _LOCK:
        try:
            return _inject_attached(pid, text)
        except Exception as e:
            return False, "ошибка инъекции: %s" % e


def _main(argv):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if len(argv) < 3:
        print("usage: terminal_inject.py <pid> <textfile>")
        return 2
    pid = argv[1]
    try:
        text = Path(argv[2]).read_text(encoding="utf-8")
    except OSError as e:
        print("не прочитал файл: %s" % e)
        return 3
    ok, msg = _inject_attached(pid, text)
    print(msg)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
