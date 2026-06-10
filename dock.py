# -*- coding: utf-8 -*-
"""Claude Usage Monitor — desktop-док «головы Клауди» (v2, гладкая отрисовка).

Безрамочная always-on-top панель вдоль края экрана. По одной «голове» (аватару)
на активного агента Claude Code. Аватары рисуются через Pillow со сглаживанием,
анимация «дыхания» — предрендер кадров и смена одной картинки (без перерисовки
холста → плавно). Данные из claude_usage.scan() (0 токенов Claude).

Свои картинки: положи PNG в avatars/<имя-проекта>.png или avatars/default.png —
будут использованы вместо встроенного аватара (обрезаются по кругу).

    python dock.py [--right] [--selftest]
    pythonw dock.py            # без консоли
"""
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import tkinter as tk
from tkinter import ttk

from PIL import Image, ImageDraw, ImageFont, ImageFilter, ImageTk

sys.path.insert(0, str(Path(__file__).parent))
import claude_usage as cu
import config
import terminal_inject

ROOT = Path(__file__).parent
AVATARS = ROOT / "avatars"
CLAUDE_BIN = config.claude_bin()

# Цвета статуса (контроллер): run=работает(оранжевый), wait=ждёт(синий), old=закрыта.
# Кольцо-обводка головы рисуется этим цветом — сразу видно кто работает, кто ждёт.
STATUS_COLOR = {"run": "#f5a623", "wait": "#5eb4ff", "old": "#6b6660"}
STATUS_LABEL = {"run": "● работает", "wait": "● ждёт ввода", "old": "○ закрыта"}
# Бейдж, если агент ждёт ИМЕННО твоего решения.
AWAIT_BADGE = {"question": "❓", "permission": "⚠", "ask": "❓"}
AWAIT_LABEL = {"question": "❓ ждёт ВЫБОРА пунктов", "permission": "⚠ ждёт подтверждения действий",
               "ask": "❓ задал вопрос «что дальше»"}


def _ps_lit(s: str) -> str:
    return (s or "").replace("'", "''")


def deliver(s: dict, prompt: str) -> str:
    """Доставить ход в сессию. Живая (есть PID) — ВПЕЧАТАТЬ прямо в её терминал
    (как будто набрал руками и нажал Enter): работа продолжается В ТОМ ЖЕ окне,
    ответ виден там же. Закрытая — фолбэк на headless `claude -p --resume`."""
    pid = s.get("pid")
    sid = s.get("session_id")
    path = s.get("project_path", "")
    if pid and s.get("status") in ("run", "wait"):
        ok, msg = terminal_inject.send_to_terminal(pid, prompt)
        if ok:
            return ("✅ Впечатано в ЖИВОЙ терминал сессии (PID %s).\n"
                    "Ответ появится в САМОМ окне сессии — это та же сессия, "
                    "контекст сохранён.\n\n(%s)" % (pid, msg))
        # Сессия живая, но впечатать не вышло (например, окно VS Code/ConPTY).
        # resume сюда НЕ делаем — это второй процесс на ту же сессию (конфликт
        # записи транскрипта). Сообщаем, чтобы юзер набрал в окне руками.
        return ("⚠ Не удалось впечатать в окно сессии (%s).\n"
                "Окно живо, но ввод не принят — набери ход прямо в нём." % msg)
    return drive_session(path, sid, prompt)


def drive_session(path: str, sid: str, prompt: str, timeout: int = 900) -> str:
    """Дописать в существующую сессию (--resume) одним ходом -p. Вернуть текст ответа."""
    ps = (f"& '{CLAUDE_BIN}' --resume {sid} -p $env:CLAUDE_PROMPT "
          f"--output-format text --permission-mode acceptEdits")
    env = {**os.environ, "CLAUDE_PROMPT": prompt}
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            cwd=path if path else None, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (r.stdout or "").strip()
        if r.returncode != 0 and r.stderr and r.stderr.strip():
            out += "\n\n[stderr]\n" + r.stderr.strip()
        return out or "(пустой ответ)"
    except subprocess.TimeoutExpired:
        return "⏱ Таймаут."
    except Exception as e:
        return f"Ошибка: {e}"

# Прозрачный цвет-ключ: тёмный, чтобы сглаженные края панели сливались с ним невидимо.
TRANSPARENT = (10, 11, 12)
TRANSPARENT_HEX = "#0a0b0c"

WIN_W = 66
PANEL_X0, PANEL_X1 = 7, 59          # панель внутри окна (поля под прозрачность)
COIN = 42                            # диаметр аватара (обычный режим)
COIN_C = 32                          # диаметр аватара (компактный режим — много сессий)
GLOW = 60                            # размер ореола
GLOW_C = 46                          # ореол в компактном режиме
CELL = 62                            # высота ячейки (обычный режим, с монограммой)
CELL_C = 40                          # высота ячейки (компактный режим, без монограммы)
COMPACT_FROM = 12                    # с этого числа живых голов — компактный режим
HEADER = 66
PAD_BOT = 12
FRAMES = 24                          # кадров «дыхания»
ANIM_MS = 40                         # ~25 fps
REFRESH = 3.0

PANEL = (27, 24, 20)
PANEL_BORDER = (60, 54, 46)
CLAUDE = (217, 119, 87)
TEXT = (244, 241, 234)
DIM = (158, 152, 142)

PALETTE = ["#D97757", "#E8A33D", "#6E9A8D", "#7E8CC4", "#B5739E",
           "#5BA3C7", "#8FA65B", "#C7674F", "#A88BC9", "#D2A24C"]

# кэши
_coin_cache: dict = {}
_glow_cache: dict = {}
_custom_cache: dict = {}
_font_cache: dict = {}


def _hex(c):
    c = c.lstrip("#")
    return (int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))


def _mix(c1, c2, t):
    a, b = _hex(c1) if isinstance(c1, str) else c1, _hex(c2) if isinstance(c2, str) else c2
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _font(size, bold=False):
    key = (size, bold)
    if key in _font_cache:
        return _font_cache[key]
    cands = ([r"C:\Windows\Fonts\seguisb.ttf", r"C:\Windows\Fonts\segoeui.ttf"]
             if bold else [r"C:\Windows\Fonts\segoeui.ttf"])
    f = None
    for p in cands:
        try:
            f = ImageFont.truetype(p, size)
            break
        except OSError:
            continue
    if f is None:
        f = ImageFont.load_default()
    _font_cache[key] = f
    return f


def strip_emoji(s: str) -> str:
    i = 0
    while i < len(s) and (ord(s[i]) > 0x2190 or s[i] in " \t"):
        i += 1
    return (s[i:].strip() or s).strip()


def color_for(project: str) -> str:
    h = 0
    for ch in project:
        h = (h * 31 + ord(ch)) & 0xFFFFFFFF
    return PALETTE[h % len(PALETTE)]


def monogram(project: str) -> str:
    parts = [p for p in strip_emoji(project).replace("/", " ").split() if p]
    if not parts:
        return "?"
    return (parts[0][:2] if len(parts) == 1 else parts[0][0] + parts[1][0]).upper()


def _slug(project: str) -> str:
    return re.sub(r"[^a-z0-9а-я]+", "-", strip_emoji(project).lower()).strip("-")


def _custom_avatar(project: str):
    if not AVATARS.exists():
        return None
    slug = _slug(project)
    if slug in _custom_cache:
        return _custom_cache[slug]
    path = None
    for cand in (AVATARS / f"{slug}.png", AVATARS / "default.png"):
        if cand.exists():
            path = cand
            break
    img = None
    if path:
        try:
            img = Image.open(path).convert("RGBA")
        except Exception:
            img = None
    _custom_cache[slug] = img
    return img


def _coin(color_hex, custom_key=None, size=COIN):
    """Аватар-«монета» с роботом, сглажен (supersample + LANCZOS)."""
    ck = (color_hex, custom_key, size)
    if ck in _coin_cache:
        return _coin_cache[ck]
    SS = 4
    S = size * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    col = _hex(color_hex)

    custom = _custom_avatar(custom_key) if custom_key else None
    if custom is not None:
        av = custom.resize((S, S), Image.LANCZOS)
        mask = Image.new("L", (S, S), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, S - 1, S - 1], fill=255)
        coin = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        coin.paste(av, (0, 0), mask)
        # тонкое кольцо
        ImageDraw.Draw(coin).ellipse([1, 1, S - 2, S - 2], outline=col + (200,), width=SS)
        img = coin
    else:
        # тело-монета + вертикальный градиент (свет сверху)
        d.ellipse([0, 0, S - 1, S - 1], fill=col + (255,))
        grad = Image.new("L", (1, S))
        for y in range(S):
            grad.putpixel((0, y), int(70 * (1 - y / S)))
        light = Image.new("RGBA", (S, S), (255, 255, 255, 0))
        light.putalpha(grad.resize((S, S)))
        mask = Image.new("L", (S, S), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, S - 1, S - 1], fill=255)
        img = Image.alpha_composite(img, Image.composite(light, Image.new("RGBA", (S, S), (0, 0, 0, 0)), mask))
        d = ImageDraw.Draw(img)
        # лёгкая тень снизу-внутри
        d.ellipse([0, 0, S - 1, S - 1], outline=(0, 0, 0, 60), width=SS)
        # антенна
        ax = S // 2
        d.line([ax, int(S * 0.16), ax, int(S * 0.07)], fill=(255, 255, 255, 235), width=SS)
        d.ellipse([ax - int(S * 0.045), int(S * 0.02), ax + int(S * 0.045), int(S * 0.11)],
                  fill=(255, 255, 255, 240))
        # визор
        vw = int(S * 0.60)
        vh = int(S * 0.30)
        vx = (S - vw) // 2
        vy = int(S * 0.40)
        d.rounded_rectangle([vx, vy, vx + vw, vy + vh], radius=int(vh * 0.45),
                            fill=(28, 25, 22, 240))
        # глаза
        er = int(vh * 0.26)
        ey = vy + vh // 2
        ex1 = vx + int(vw * 0.30)
        ex2 = vx + int(vw * 0.70)
        eye = _mix(color_hex, "#ffffff", 0.55)
        for ex in (ex1, ex2):
            d.ellipse([ex - er, ey - er, ex + er, ey + er], fill=eye + (255,))

    img = img.resize((size, size), Image.LANCZOS)
    _coin_cache[ck] = img
    return img


def _glow(color_hex, size=GLOW):
    key = (color_hex, size)
    if key in _glow_cache:
        return _glow_cache[key]
    SS = 2
    S = size * SS
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = int(S * 0.40)
    d.ellipse([S // 2 - r, S // 2 - r, S // 2 + r, S // 2 + r], fill=_hex(color_hex) + (190,))
    img = img.filter(ImageFilter.GaussianBlur(S * 0.075))
    img = img.resize((size, size), Image.LANCZOS)
    _glow_cache[key] = img
    return img


def _alpha_scaled(im, k):
    a = im.split()[3].point(lambda p: int(p * k))
    out = im.copy()
    out.putalpha(a)
    return out


def _age_str(sec: int) -> str:
    if sec < 45:
        return "только что"
    if sec < 3600:
        return f"{sec // 60} мин назад"
    if sec < 86400:
        return f"{sec // 3600} ч назад"
    return f"{sec // 86400} дн назад"


class Dock:
    def __init__(self, side="left"):
        self.side = side
        self.root = tk.Tk()
        self.root.title("Claude Dock")
        self.root.overrideredirect(True)
        self.root.wm_attributes("-topmost", True)
        try:
            self.root.wm_attributes("-transparentcolor", TRANSPARENT_HEX)
        except tk.TclError:
            pass
        self.root.config(bg=TRANSPARENT_HEX)
        self.sw = self.root.winfo_screenwidth()
        self.sh = self.root.winfo_screenheight()

        self.canvas = tk.Canvas(self.root, bg=TRANSPARENT_HEX, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.img_item = self.canvas.create_image(0, 0, anchor="nw")

        self.snapshot = None
        self.frames = []          # PhotoImage
        self.regions = []
        self.idx = 0
        self._pending = None      # (snap, pil_frames, regions, (w,h))
        self.detail = None
        self.tooltip = None
        self.hover_id = None
        self.show_idle = False
        self._win_x = 0
        self._win_y = 0

        self._build_menu()
        self.canvas.bind("<Motion>", self.on_motion)
        self.canvas.bind("<Button-1>", self.on_click)
        self.canvas.bind("<Button-3>", self.on_right)
        self.canvas.bind("<Leave>", lambda e: self.hide_tooltip())

        threading.Thread(target=self._scan_loop, daemon=True).start()
        self._animate()

    # ---------- меню ----------
    def _build_menu(self):
        self.menu = tk.Menu(self.root, tearoff=0)
        self.menu.add_command(label="Открыть полное окно", command=lambda: self.open_detail(None))
        self.menu.add_command(label="Обновить сейчас", command=self._scan_once_async)
        self.menu.add_separator()
        self.menu.add_command(label="Сторона: лево/право", command=self.flip_side)
        self.menu.add_command(label="Показывать неактивные: вкл/выкл", command=self.toggle_idle)
        self.menu.add_separator()
        self.menu.add_command(label="Выход", command=self.root.destroy)

    def on_right(self, e):
        s = self._hit(e.x, e.y)
        m = tk.Menu(self.root, tearoff=0)
        if isinstance(s, dict):
            m.add_command(label="▸ " + strip_emoji(s["project"]) + "  ["
                          + STATUS_LABEL.get(s.get("status", "old"), "") + "]", state="disabled")
            m.add_command(label="✍ Написать в сессию (та же, resume)",
                          command=lambda ss=s: self._write_to_session(ss))
            m.add_command(label="Открыть папку проекта",
                          command=lambda p=s.get("project_path"): self._open_folder(p))
            m.add_command(label="Новая сессия в проекте",
                          command=lambda p=s.get("project_path"), n=s.get("project", "session"): self._new_session(p, n))
            m.add_command(label="Полное окно (выделить)",
                          command=lambda i=s["session_id"]: self.open_detail(i))
            m.add_command(label="Копировать session-id",
                          command=lambda i=s["session_id"]: self._copy(i))
            m.add_separator()
        m.add_command(label="Открыть полное окно", command=lambda: self.open_detail(None))
        m.add_command(label="Обновить сейчас", command=self._scan_once_async)
        m.add_command(label="Сторона: лево/право", command=self.flip_side)
        m.add_command(label="Показывать неактивные", command=self.toggle_idle)
        m.add_separator()
        m.add_command(label="Выход", command=self.root.destroy)
        # меню сбоку от полосы, не под головами и не за экраном
        px = self._win_x + WIN_W + 4 if self.side == "left" else max(0, self._win_x - 200)
        py = min(e.y_root, self.sh - 240)
        try:
            m.tk_popup(px, py)
        finally:
            m.grab_release()

    def _open_folder(self, path):
        if path:
            try:
                import os
                os.startfile(path)
            except Exception as ex:
                print("open folder:", ex, file=sys.stderr)

    def _copy(self, text):
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(text or "")
        except tk.TclError:
            pass

    def _new_session(self, path, name="session"):
        if not path:
            return
        try:
            # --session-id чтобы потом можно было ДОПИСЫВАТЬ именно в эту сессию;
            # без -NoExit окно закрывается само при выходе из claude (нет зомби).
            sid = str(uuid.uuid4())
            ps = (f"Set-Location -LiteralPath '{_ps_lit(path)}'; "
                  f"& '{CLAUDE_BIN}' --session-id {sid} -n '{_ps_lit(name)[:40]}'")
            subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                              "-Command", ps], creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        except Exception as ex:
            print("new session:", ex, file=sys.stderr)

    def _write_to_session(self, s):
        """Окошко: дописать ход в существующую сессию (--resume) + история команд."""
        if not s:
            return
        sid = s.get("session_id")
        path = s.get("project_path", "")
        cu.mark_seen(sid)          # открыли диалог — ответ просмотрен
        win = tk.Toplevel(self.root)
        win.title("Написать в сессию — " + strip_emoji(s["project"]))
        win.configure(bg="#17150f")
        win.geometry("680x540")
        win.wm_attributes("-topmost", True)
        pc = color_for(s["project"])
        tk.Label(win, text=f"{strip_emoji(s['project'])}   [{STATUS_LABEL.get(s.get('status','old'))}]",
                 bg="#17150f", fg=pc, font=("Segoe UI", 11, "bold"), anchor="w").pack(fill="x", padx=12, pady=(10, 2))
        tk.Label(win, text="id …" + (sid or "")[-12:] + "    " + (s.get("title", "")[:70]),
                 bg="#17150f", fg="#9a948a", font=("Segoe UI", 8), anchor="w").pack(fill="x", padx=12)

        entry = tk.Text(win, height=3, bg="#201d17", fg="#f4f1ea", insertbackground="#f4f1ea",
                        font=("Segoe UI", 10), wrap="word", bd=0)
        out = tk.Text(win, height=11, bg="#100e0a", fg="#f4f1ea", insertbackground="#f4f1ea",
                      font=("Consolas", 9), wrap="word", bd=0)

        hist = s.get("history", [])
        if hist:
            hf = tk.Frame(win, bg="#201d17")
            hf.pack(fill="x", padx=12, pady=6)
            tk.Label(hf, text="История команд (клик — подставить):", bg="#201d17", fg="#e8a33d",
                     font=("Segoe UI", 8, "bold"), anchor="w").pack(fill="x", padx=8, pady=(4, 0))
            for h in hist[::-1]:
                lb = tk.Label(hf, text="• " + h["title"][:80], bg="#201d17", fg="#cfc8bd",
                              font=("Segoe UI", 8), anchor="w", cursor="hand2")
                lb.pack(fill="x", padx=10)
                lb.bind("<Button-1>", lambda e, t=h["title"]: (entry.delete("1.0", "end"), entry.insert("1.0", t)))

        out.pack(fill="both", expand=True, padx=12, pady=6)
        lr = s.get("last_response")
        if lr:
            out.insert("end", "── последний ответ агента ──\n" + lr + "\n\n")
        live = bool(s.get("pid") and s.get("status") in ("run", "wait"))
        out.insert("end", ("Текст будет ВПЕЧАТАН прямо в окно сессии (как будто набрал и нажал Enter) — "
                           "ответ появится в самом окне.\n" if live else
                           "Сессия закрыта — отправлю через resume, ответ придёт сюда.\n"))
        out.configure(state="disabled")
        entry.pack(fill="x", padx=12)
        btnrow = tk.Frame(win, bg="#17150f")
        btnrow.pack(fill="x", padx=12, pady=8)

        def do_send():
            prompt = entry.get("1.0", "end").strip()
            if not prompt:
                return
            send_btn.configure(state="disabled", text="Отправляю…")
            out.configure(state="normal")
            out.delete("1.0", "end")
            out.insert("end", "→ " + prompt + "\n\n…\n")
            out.configure(state="disabled")

            def work():
                resp = deliver(s, prompt)

                def show():
                    out.configure(state="normal")
                    out.delete("1.0", "end")
                    out.insert("end", "→ " + prompt + "\n\n" + resp)
                    out.configure(state="disabled")
                    send_btn.configure(state="normal", text="▶ Отправить")
                    entry.delete("1.0", "end")
                try:
                    win.after(0, show)
                except tk.TclError:
                    pass
            threading.Thread(target=work, daemon=True).start()

        def cmd(c):
            entry.delete("1.0", "end")
            entry.insert("1.0", c)
            do_send()

        send_btn = tk.Button(btnrow, text="▶ Отправить", command=do_send, bg=pc, fg="#1b1410",
                             font=("Segoe UI", 10, "bold"), bd=0, padx=14, pady=6)
        send_btn.pack(side="right")
        tk.Button(btnrow, text="🗜 /compact", command=lambda: cmd("/compact"), bg="#272219",
                  fg="#f4f1ea", bd=0, padx=10, pady=6).pack(side="left")
        tk.Button(btnrow, text="🧹 /clear", command=lambda: cmd("/clear"), bg="#272219",
                  fg="#f4f1ea", bd=0, padx=10, pady=6).pack(side="left", padx=(8, 0))
        entry.focus_set()

    def flip_side(self):
        self.side = "right" if self.side == "left" else "left"
        if self._geo:
            self._place(*self._geo)

    def toggle_idle(self):
        self.show_idle = not self.show_idle
        self._scan_once_async()

    # ---------- данные ----------
    def _scan_loop(self):
        while True:
            self._scan_once()
            time.sleep(REFRESH)

    def _scan_once_async(self):
        threading.Thread(target=self._scan_once, daemon=True).start()

    def _scan_once(self):
        try:
            snap = cu.scan()
        except Exception as ex:
            print("scan error:", ex, file=sys.stderr)
            return
        self.snapshot = snap
        try:
            frames, regions, size = self._build_pil(snap)
            self._pending = (snap, frames, regions, size)
        except Exception as ex:
            print("render error:", ex, file=sys.stderr)

    def _select_heads(self, snap):
        sessions = snap["sessions"]
        run = [s for s in sessions if s.get("status") == "run"]
        wait = [s for s in sessions if s.get("status") == "wait"]
        # ждущие: сперва кто требует решения (вопрос/выбор), затем непрочитанные, затем свежие
        wait.sort(key=lambda s: (0 if s.get("awaiting") else 1,
                                 0 if s.get("unread") else 1, s.get("age_sec", 0)))
        live = run + wait                       # работающие ВСЕГДА сверху
        if self.show_idle:
            live = live + [s for s in sessions if s.get("status") == "old"]
        n_active = len(run)
        if not live:
            return (sessions[:1] if sessions else []), n_active, 0, False
        avail = self.sh - HEADER - PAD_BOT - 18
        compact = len(live) >= COMPACT_FROM     # много сессий — мелкие головы без подписей
        cell = CELL_C if compact else CELL
        cap = max(1, avail // cell)
        if len(live) <= cap:
            return live, n_active, 0, compact
        # переполнение — оставляем последний слот под индикатор "+N"
        heads = live[:cap - 1]
        return heads, n_active, len(live) - len(heads), compact

    # ---------- отрисовка (PIL) ----------
    def _build_pil(self, snap):
        heads, n_active, overflow, compact = self._select_heads(snap)
        if not heads:
            heads = [None]
        CS = COIN_C if compact else COIN        # размер аватара
        GS = GLOW_C if compact else GLOW
        cell = CELL_C if compact else CELL
        show_mono = not compact
        n = len(heads)
        extra = 22 if overflow else 0
        h = HEADER + n * cell + extra + PAD_BOT
        w = WIN_W

        base = Image.new("RGBA", (w, h), TRANSPARENT + (255,))
        d = ImageDraw.Draw(base)
        d.rounded_rectangle([PANEL_X0, 4, PANEL_X1, h - 5], radius=16, fill=PANEL + (255,))
        d.rounded_rectangle([PANEL_X0, 4, PANEL_X1, h - 5], radius=16, outline=PANEL_BORDER + (255,), width=1)

        # шапка: «N работают (оранжевый) из M живых» + токены за сегодня.
        # N (работают) прыгает — это норма (агент busy лишь доли секунды на ход);
        # M (живых) стабильно = сколько окон открыто.
        cx = w // 2
        f1 = _font(15, True)
        f2 = _font(8, True)
        d.ellipse([cx - 15, 10, cx + 15, 40], fill=(43, 42, 40, 255),
                  outline=_hex(STATUS_COLOR["run"]) + (255,), width=2)
        cnt = str(n_active)
        tb = d.textbbox((0, 0), cnt, font=f1)
        d.text((cx - (tb[2] - tb[0]) / 2, 25 - (tb[3] - tb[1]) / 2 - tb[1]), cnt, font=f1,
               fill=_hex(STATUS_COLOR["run"]) + (255,))
        sub = "из %d" % snap.get("live_count", 0)
        tb = d.textbbox((0, 0), sub, font=f2)
        d.text((cx - (tb[2] - tb[0]) / 2, 43), sub, font=f2, fill=(158, 152, 142, 255))
        today = cu.human_tokens(snap["today"]["tokens_total"])
        tb = d.textbbox((0, 0), today, font=f2)
        d.text((cx - (tb[2] - tb[0]) / 2, 54), today, font=f2, fill=(207, 200, 189, 255))

        coins, glows, regions = [], [], []
        y = HEADER
        fm = _font(8, True)
        fb = _font(8, True)
        for s in heads:
            cy = y + cell // 2 - (4 if show_mono else 0)
            if s is None:
                coins.append((_coin("#4a4640", None, CS), cx - CS // 2, cy - CS // 2))
                glows.append(None)
                d.text((cx - 6, cy + CS // 2 + 6), "..", font=fm, fill=DIM + (255,))
                regions.append((0, y, w, y + cell, None))
                y += cell
                continue
            proj_col = color_for(s["project"])
            status = s.get("status", "old")
            live = status in ("run", "wait")
            ccol = proj_col if live else "#%02x%02x%02x" % _mix(proj_col, "#4a463f", 0.62)
            coin = _coin(ccol, s["project"] if _custom_avatar(s["project"]) is not None else None, CS)
            coins.append((coin, cx - CS // 2, cy - CS // 2))
            glows.append(_glow(STATUS_COLOR[status], GS) if live else None)
            # КОЛЬЦО-ОБВОДКА статусом: оранжевый=работает, синий=ждёт
            if live:
                rr = CS // 2 + 3
                d.ellipse([cx - rr, cy - rr, cx + rr, cy + rr],
                          outline=_hex(STATUS_COLOR[status]) + (255,), width=(2 if compact else 3))
            # монограмма (только в обычном режиме)
            if show_mono:
                mono = monogram(s["project"])
                tb = d.textbbox((0, 0), mono, font=fm)
                d.text((cx - (tb[2] - tb[0]) / 2, cy + CS // 2 + 6), mono, font=fm,
                       fill=(TEXT if live else DIM) + (255,))
            # бейдж субагентов (низ-лево)
            if s.get("subagents"):
                bx, by = cx - CS // 2 + 1, cy + CS // 2 - 3
                d.ellipse([bx - 7, by - 7, bx + 7, by + 7], fill=(43, 42, 40, 255),
                          outline=_hex(proj_col) + (255,))
                t = str(s["subagents"])
                tb = d.textbbox((0, 0), t, font=fb)
                d.text((bx - (tb[2] - tb[0]) / 2, by - (tb[3] - tb[1]) / 2 - tb[1]), t, font=fb, fill=TEXT + (255,))
            # бейдж «ждёт ТВОЕГО решения» (верх-лево): ❓ выбор пунктов / ⚠ действия
            aw = s.get("awaiting")
            if aw:
                bx, by = cx - CS // 2 + 1, cy - CS // 2 + 1
                bcol = (235, 70, 55) if aw == "permission" else (245, 166, 35)
                d.ellipse([bx - 8, by - 8, bx + 8, by + 8], fill=bcol + (255,), outline=(20, 18, 16, 255))
                bd = AWAIT_BADGE.get(aw, "!")
                tb = d.textbbox((0, 0), bd, font=fb)
                d.text((bx - (tb[2] - tb[0]) / 2, by - (tb[3] - tb[1]) / 2 - tb[1]), bd, font=fb, fill=(255, 255, 255, 255))
            # точка «новый ответ» (верх-право)
            if s.get("unread"):
                bx, by = cx + CS // 2 - 1, cy - CS // 2 + 1
                d.ellipse([bx - 5, by - 5, bx + 5, by + 5], fill=(235, 64, 52, 255), outline=(20, 18, 16, 255))
            regions.append((0, y, w, y + cell, s))
            y += cell

        # индикатор переполнения "+N" (клик → полное окно)
        if overflow:
            ty = y + 3
            txt = "+%d" % overflow
            ff = _font(11, True)
            tb = d.textbbox((0, 0), txt, font=ff)
            d.rounded_rectangle([cx - 17, ty, cx + 17, ty + 17], radius=8, fill=(70, 63, 53, 255))
            d.text((cx - (tb[2] - tb[0]) / 2, ty + 9 - (tb[3] - tb[1]) / 2 - tb[1]), txt, font=ff, fill=TEXT + (255,))
            regions.append((0, y, w, y + extra, "__overflow__"))

        frames = []
        for f in range(FRAMES):
            t = f / FRAMES
            inten = 0.30 + 0.45 * (0.5 * (1 + math.sin(2 * math.pi * t)))
            fr = base.copy()
            for (coin, cxp, cyp), g in zip(coins, glows):
                if g is not None:
                    gg = _alpha_scaled(g, inten)
                    fr.alpha_composite(gg, (cx - GS // 2, cyp + CS // 2 - GS // 2))
                fr.alpha_composite(coin, (cxp, cyp))
            frames.append(fr)
        return frames, regions, (w, h)

    # ---------- размещение ----------
    _geo = None

    def _place(self, w, h):
        self._geo = (w, h)
        x = 0 if self.side == "left" else self.sw - w
        y = max(10, (self.sh - h) // 2)
        self._win_x, self._win_y = x, y
        self.root.geometry(f"{w}x{h}+{x}+{y}")

    # ---------- анимация ----------
    def _animate(self):
        try:
            if self._pending is not None:
                snap, pil, regions, size = self._pending
                self._pending = None
                self.frames = [ImageTk.PhotoImage(p) for p in pil]
                self.regions = regions
                self.idx = 0
                self._place(*size)
            if self.frames:
                self.idx = (self.idx + 1) % len(self.frames)
                self.canvas.itemconfigure(self.img_item, image=self.frames[self.idx])
            self.root.wm_attributes("-topmost", True)
        except tk.TclError:
            return
        self.root.after(ANIM_MS, self._animate)

    # ---------- интерактив ----------
    def _hit(self, x, y):
        for (x0, y0, x1, y1, s) in self.regions:
            if x0 <= x <= x1 and y0 <= y <= y1:
                return s
        return None

    def on_motion(self, e):
        s = self._hit(e.x, e.y)
        if not isinstance(s, dict):
            self.hide_tooltip()
            self.hover_id = None
            return
        if s["session_id"] != self.hover_id:
            self.hover_id = s["session_id"]
            self.show_tooltip(s, e.y)

    def show_tooltip(self, s, wy):
        self.hide_tooltip()
        tip = tk.Toplevel(self.root)
        tip.overrideredirect(True)
        tip.wm_attributes("-topmost", True)
        try:
            tip.attributes("-alpha", 0.97)
        except tk.TclError:
            pass
        pc = color_for(s["project"])
        frame = tk.Frame(tip, bg="#1f1d1a", highlightbackground=pc,
                         highlightthickness=1, bd=0)
        frame.pack()
        proj = strip_emoji(s["project"]) + (f"  +{s['subagents']} sub" if s["subagents"] else "")
        rows = [
            (proj, ("Segoe UI", 10, "bold"), pc),
            (s["title"][:64], ("Segoe UI", 9), "#f4f1ea"),
            (f"{cu.human_tokens(s['tokens_total'])} токенов  ·  ≈{cu.human_cost(s['cost_usd'])}",
             ("Segoe UI", 9, "bold"), "#cfc8bd"),
            (f"in {cu.human_tokens(s['tokens']['input'])} · out {cu.human_tokens(s['tokens']['output'])}"
             f" · cache {cu.human_tokens(s['tokens']['cache_read'] + s['tokens']['cache_write'])}",
             ("Segoe UI", 8), "#9a948a"),
            (f"{' / '.join(s['models'][:2])} · {s['msg_count']} ответов · {_age_str(s['age_sec'])}",
             ("Segoe UI", 8), "#9a948a"),
            (STATUS_LABEL.get(s.get("status", "old"), "○"),
             ("Segoe UI", 9, "bold"), STATUS_COLOR.get(s.get("status", "old"), "#9a948a")),
        ]
        for txt, font, col in rows:
            tk.Label(frame, text=txt, bg="#1f1d1a", fg=col, font=font, anchor="w",
                     justify="left", wraplength=330).pack(fill="x", padx=10, pady=1)
        # ЖДЁТ ТВОЕГО РЕШЕНИЯ — выделить (вопрос/выбор пунктов/подтверждение)
        aw = s.get("awaiting")
        if aw:
            tk.Label(frame, text=AWAIT_LABEL.get(aw, "● ждёт ответа"), bg="#1f1d1a",
                     fg="#ffcf7a", font=("Segoe UI", 9, "bold"), anchor="w",
                     justify="left", wraplength=330).pack(fill="x", padx=10, pady=(4, 1))
            det = s.get("awaiting_detail")
            if aw == "question" and isinstance(det, list):
                for q in det[:2]:
                    opts = " · ".join((q.get("options") or [])[:4])
                    line = "• " + (q.get("q", "")[:64]) + (("  → " + opts) if opts else "")
                    tk.Label(frame, text=line, bg="#1f1d1a", fg="#e8d9b0",
                             font=("Segoe UI", 8), anchor="w", justify="left",
                             wraplength=330).pack(fill="x", padx=14)
            elif aw == "permission" and det:
                tk.Label(frame, text="действия: " + ", ".join(det[:4]), bg="#1f1d1a",
                         fg="#e8d9b0", font=("Segoe UI", 8), anchor="w",
                         justify="left", wraplength=330).pack(fill="x", padx=14)
        # последний ответ агента (по просьбе — показывать при наведении/клике)
        lr = s.get("last_response")
        if lr:
            tk.Label(frame, text="── последний ответ ──", bg="#1f1d1a", fg="#6f6a62",
                     font=("Segoe UI", 7, "bold"), anchor="w").pack(fill="x", padx=10, pady=(4, 0))
            tk.Label(frame, text=lr[:240] + ("…" if len(lr) > 240 else ""), bg="#1f1d1a",
                     fg="#c9c2b6", font=("Segoe UI", 8), anchor="w", justify="left",
                     wraplength=330).pack(fill="x", padx=10)
        # история команд (последние, без мусора)
        hist = s.get("history", [])
        if hist:
            tk.Label(frame, text="── история команд ──", bg="#1f1d1a", fg="#6f6a62",
                     font=("Segoe UI", 7, "bold"), anchor="w").pack(fill="x", padx=10, pady=(4, 0))
            for h in hist[-6:][::-1]:
                tk.Label(frame, text="• " + h["title"][:60], bg="#1f1d1a", fg="#b9b3a8",
                         font=("Segoe UI", 8), anchor="w", justify="left",
                         wraplength=330).pack(fill="x", padx=10, pady=0)
        tk.Label(frame, text="ЛКМ — все сессии · ПКМ — написать в сессию", bg="#1f1d1a",
                 fg="#6f6a62", font=("Segoe UI", 7), anchor="w").pack(fill="x", padx=10, pady=(4, 2))
        tip.update_idletasks()
        w, h = tip.winfo_width(), tip.winfo_height()
        # привязка к полосе (не к курсору): сбоку от дока, на уровне головы — без обрезки
        x = self._win_x + WIN_W + 8 if self.side == "left" else self._win_x - w - 8
        y = max(6, min(self._win_y + wy - h // 2, self.sh - h - 10))
        tip.geometry(f"+{x}+{y}")
        self.tooltip = tip

    def hide_tooltip(self):
        if self.tooltip is not None:
            try:
                self.tooltip.destroy()
            except tk.TclError:
                pass
            self.tooltip = None

    def on_click(self, e):
        s = self._hit(e.x, e.y)
        if isinstance(s, dict):
            cu.mark_seen(s.get("session_id"))      # клик снимает «непрочитано»
            self.open_detail(s["session_id"])
            self._scan_once_async()
        else:
            self.open_detail(None)

    # ---------- полное окно ----------
    def open_detail(self, highlight_sid):
        self.hide_tooltip()
        if self.detail is not None:
            try:
                self.detail.destroy()
            except tk.TclError:
                pass
        snap = self.snapshot or cu.scan()
        win = tk.Toplevel(self.root)
        self.detail = win
        win.title("Claude Usage — все сессии")
        win.configure(bg="#17150f")
        win.geometry("1000x640")
        win.wm_attributes("-topmost", True)

        t = snap["today"]
        tot = snap["totals"]
        run_n = sum(1 for x in snap['sessions'] if x.get('status') == 'run')
        head = (f"Работают: {run_n}   Живых: {snap.get('live_count', 0)}   "
                f"Ждут решения: {snap.get('attention_count', 0)}      "
                f"Сегодня: {cu.human_tokens(t['tokens_total'])} ток ≈{cu.human_cost(t['cost_usd'])}      "
                f"Всего(на диске): {cu.human_tokens(tot['tokens_total'])} ≈{cu.human_cost(tot['cost_usd'])}")
        tk.Label(win, text="✳ ClaudeDock", bg="#17150f", fg="#f5a623",
                 font=("Segoe UI", 13, "bold"), anchor="w").pack(fill="x", padx=14, pady=(11, 0))
        tk.Label(win, text=head, bg="#17150f", fg="#f4f1ea",
                 font=("Segoe UI", 11, "bold"), anchor="w").pack(fill="x", padx=14, pady=(2, 2))
        tk.Label(win, text="🟧 работает · 🟦 ждёт · ❓ ждёт ВЫБОРА пунктов · ⚠ ждёт подтверждения действий · 🔴 новый ответ    —    двойной клик: написать в сессию (resume)",
                 bg="#17150f", fg="#9a948a", font=("Segoe UI", 8), anchor="w").pack(fill="x", padx=14)

        daily = snap.get("daily", {})
        if daily:
            days = sorted(daily.keys())[-14:]
            spark = cu._spark([daily[d]["tokens"] for d in days])
            tk.Label(win, text=f"Динамика 14 дн:  {spark}", bg="#17150f", fg="#cfc8bd",
                     font=("Consolas", 11), anchor="w").pack(fill="x", padx=12, pady=(4, 6))

        style = ttk.Style(win)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.layout("CU.Treeview", [("CU.Treeview.treearea", {"sticky": "nswe"})])
        style.configure("CU.Treeview", background="#1e1b15", foreground="#f4f1ea",
                        fieldbackground="#1e1b15", rowheight=31, borderwidth=0,
                        relief="flat", font=("Segoe UI", 10))
        style.map("CU.Treeview",
                  background=[("selected", "#4a3f23")],
                  foreground=[("selected", "#fff7e6")])
        style.configure("CU.Treeview.Heading", background="#26221b", foreground="#e8a33d",
                        font=("Segoe UI", 9, "bold"), relief="flat",
                        borderwidth=0, padding=(10, 9))
        style.map("CU.Treeview.Heading",
                  background=[("active", "#332d23")],
                  foreground=[("active", "#ffc05a")])
        style.configure("CU.Vertical.TScrollbar", troughcolor="#1b1812",
                        background="#3a3324", borderwidth=0, arrowcolor="#1b1812",
                        width=12, gripcount=0)
        style.map("CU.Vertical.TScrollbar", background=[("active", "#5a4d2c")])
        style.layout("CU.Vertical.TScrollbar", [
            ("CU.Vertical.TScrollbar.trough", {"sticky": "ns", "children": [
                ("CU.Vertical.TScrollbar.thumb", {"expand": "1", "sticky": "nswe"})]})])

        cols = ("status", "project", "task", "tokens", "cost", "model", "msgs", "age")
        tree = ttk.Treeview(win, columns=cols, show="headings", style="CU.Treeview")
        headers = {"status": ("Статус", 152), "project": ("Проект", 140), "task": ("Задача", 250),
                   "tokens": ("Токены", 84), "cost": ("≈$", 66), "model": ("Модель", 104),
                   "msgs": ("Ответы", 64), "age": ("Возраст", 92)}
        right = ("tokens", "cost", "msgs", "age")
        for c_ in cols:
            txt, w = headers[c_]
            tree.heading(c_, text=txt,
                         anchor="w" if c_ in ("status", "project", "task") else "e" if c_ in right else "center")
            anchor = "w" if c_ in ("status", "project", "task") else "e" if c_ in right else "center"
            tree.column(c_, width=w, anchor=anchor, stretch=(c_ == "task"))
        tree.tag_configure("run", foreground="#f5c451")
        tree.tag_configure("wait", foreground="#5eb4ff")
        tree.tag_configure("old", foreground="#8f8980")
        tree.tag_configure("evenrow", background="#1e1b15")
        tree.tag_configure("oddrow", background="#242017")
        tree.tag_configure("hl", background="#4a3f23")

        self._detail_map = {}
        sel_item = None
        for idx, s in enumerate(snap["sessions"]):
            st = s.get("status", "old")
            tags = ["evenrow" if idx % 2 == 0 else "oddrow", st]
            if s["session_id"] == highlight_sid:
                tags.append("hl")
            age = s["age_sec"]
            age_s = (f"{age}s" if age < 90 else (f"{age // 60}m" if age < 5400 else
                     (f"{age // 3600}h" if age < 172800 else f"{age // 86400}d")))
            stv = " " + STATUS_LABEL.get(st, "○")
            aw = s.get("awaiting")
            if aw:
                stv += {"question": " ❓", "permission": " ⚠", "ask": " ❓"}.get(aw, "")
            if s.get("unread"):
                stv = "🔴 " + stv
            item = tree.insert("", "end", values=(
                stv, strip_emoji(s["project"]), s["title"][:72],
                cu.human_tokens(s["tokens_total"]), cu.human_cost(s["cost_usd"]),
                "/".join(s["models"][:2]), s["msg_count"], age_s), tags=tags)
            self._detail_map[item] = s
            if s["session_id"] == highlight_sid:
                sel_item = item

        vsb = ttk.Scrollbar(win, orient="vertical", command=tree.yview, style="CU.Vertical.TScrollbar")
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True, padx=(14, 10), pady=(2, 14))
        vsb.pack(side="right", fill="y", pady=(2, 14), padx=(0, 12))
        if sel_item:
            tree.selection_set(sel_item)
            tree.see(sel_item)

        def _on_dbl(e):
            it = tree.identify_row(e.y)
            if it and it in self._detail_map:
                self._write_to_session(self._detail_map[it])
        tree.bind("<Double-1>", _on_dbl)

    def run(self):
        self.root.mainloop()


def main():
    side = "right" if "--right" in sys.argv else "left"
    if "--selftest" in sys.argv:
        d = Dock(side=side)
        d._scan_once()
        d.root.after(1200, d.root.destroy)
        d.run()
        print("selftest OK")
        return
    Dock(side=side).run()


if __name__ == "__main__":
    main()
