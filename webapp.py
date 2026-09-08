# -*- coding: utf-8 -*-
"""Claude Usage Monitor — веб-приложение (live-вью + управление).

PC-сервер (stdlib). Отдаёт:
  GET  /cu/<token>/                 — страница Mini App (miniapp.html)
  GET  /cu/<token>/api/usage        — live данные (scan, 0 токенов Claude)
  GET  /cu/<token>/api/projects     — недавние проекты для запуска
  POST /cu/<token>/api/run          — управление: новая сессия / headless
                                      (требует подписи Telegram initData = владелец)
  POST /cu/<token>/api/upload       — вложение: файл кладётся на ПК, путь уходит в промт
  GET  /cu/<token>/api/file?id=…    — отдать доставленный файл обратно (превью)

Снаружи доступен через reverse-туннель PC:8765 -> VPS, nginx проксирует /cu/.
Управление защищено двойным фактором: секретный токен в пути + проверка
Telegram WebApp initData (HMAC по токену бота, user.id == владелец).

    python webapp.py            # 127.0.0.1:8765
    python webapp.py --selftest
"""
import hashlib
import hmac
import base64
import json
import mimetypes
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlparse

sys.path.insert(0, str(Path(__file__).parent))
import claude_usage as cu
import config
import terminal_inject
from tg import send as tg_send, TOKEN as BOT_TOKEN, CHAT_ID

TOKEN = config.web_url_secret()             # secret in the URL path (auto-generated)
OWNER_ID = int(CHAT_ID) if CHAT_ID else None  # None => remote control stays owner-locked off
HOST = config.WEB_HOST
PORT = config.WEB_PORT
BASE = f"/cu/{TOKEN}"
ROOT = Path(__file__).parent
MINIAPP = ROOT / "miniapp.html"

CLAUDE = config.claude_bin()
WEB_PASSWORD = config.get("web_password")   # optional: password login for remote control without Telegram
WEB_TRUST_KEY = config.get("web_trust_key")  # a reverse proxy behind its OWN auth (e.g. nginx htpasswd) sends
VPS = config.get("vps") or {}               # optional: launch/connect a session on a remote server over SSH
                                             # header X-ClaudeDock-Auth: <key> to vouch for the request -> control


# ---------------------------------------------------------------------------
# Attachments delivered from the phone
#
# The point is not to show a picture in the web page: it is to put the file on
# the PC and hand the agent a path it can open. So an upload lands in data/inbox
# and /api/chat is sent the absolute path along with the text.
# ---------------------------------------------------------------------------
INBOX = ROOT / "data" / "inbox"
MAX_UPLOAD = 25 * 1024 * 1024        # nginx /cu/ is set to the same 25M
INBOX_KEEP_DAYS = 30


def _safe_name(name: str) -> str:
    """A file name safe to write on Windows and safe to type into a console."""
    base = os.path.basename((name or "").replace("\\", "/")).strip()
    stem, ext = os.path.splitext(base)
    clean = lambda t: "".join(c if (c.isalnum() or c in "._-") else "_" for c in t)
    stem = clean(stem).strip("._")[:48] or "file"
    ext = clean(ext).lower()[:12]
    return stem + ext


def _prune_inbox(days: int = INBOX_KEEP_DAYS) -> None:
    """Drop day folders older than `days` — the inbox is a hand-off, not an archive."""
    cutoff = time.time() - days * 86400
    try:
        for d in INBOX.iterdir():
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
    except OSError:
        pass


def save_attachment(raw: bytes, filename: str) -> dict:
    """Write one delivered file into the inbox and say where it landed."""
    day = time.strftime("%Y-%m-%d")
    folder = INBOX / day
    folder.mkdir(parents=True, exist_ok=True)
    stamp = "%s_%s" % (time.strftime("%H%M%S"), uuid.uuid4().hex[:4])
    f = folder / ("%s_%s" % (stamp, _safe_name(filename)))
    f.write_bytes(raw)
    _prune_inbox()
    return {"name": f.name, "id": day + "/" + f.name, "path": str(f), "size": len(raw)}


def inbox_file(fid: str):
    """Resolve an inbox id (`<day>/<name>`) to a real file inside the inbox."""
    parts = (fid or "").split("/")
    if len(parts) != 2 or any((not x) or x in (".", "..") or "\\" in x for x in parts):
        return None
    try:
        f = (INBOX / parts[0] / parts[1]).resolve()
        if f.is_file() and INBOX.resolve() in f.parents:
            return f
    except OSError:
        pass
    return None


def _session_value():
    """Opaque session-cookie value derived from the password (no server-side state)."""
    key = (WEB_PASSWORD or TOKEN).encode("utf-8")
    return hmac.new(key, b"claudedock-auth-v1", hashlib.sha256).hexdigest()


# ---------------------------------------------------------------------------
# Telegram WebApp initData verification
# ---------------------------------------------------------------------------
def verify_init_data(init_data: str):
    """Проверить подпись Telegram WebApp. Вернуть user dict или None."""
    if not init_data:
        return None
    try:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        recv_hash = pairs.pop("hash", None)
        if not recv_hash:
            return None
        data_check = "\n".join(f"{k}={pairs[k]}" for k in sorted(pairs))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calc = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(calc, recv_hash):
            return None
        # свежесть: не старше 24ч
        try:
            if time.time() - int(pairs.get("auth_date", "0")) > 86400:
                return None
        except ValueError:
            pass
        return json.loads(pairs.get("user", "{}"))
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Данные и управление
# ---------------------------------------------------------------------------
def recent_projects(limit=16):
    snap = cu.scan()
    seen = {}
    for s in snap["sessions"]:
        p = s.get("project_path")
        if p and p not in seen:
            seen[p] = s["project"]
        if len(seen) >= limit:
            break
    return [{"name": n, "path": p} for p, n in seen.items()]


def _ps_lit(s: str) -> str:
    """Экранировать строку для одинарных кавычек PowerShell."""
    return (s or "").replace("'", "''")


def start_session(path: str, task: str, name: str = "session") -> str:
    """Открыть новую ИНТЕРАКТИВНУЮ сессию claude в проекте (отдельное окно).

    Ключевые отличия от старой версии:
    • Заранее генерим session-id (--session-id) → контроллер сразу знает транскрипт
      и может позже ДОПИСЫВАТЬ именно в эту сессию (--resume), а не плодить новые.
    • БЕЗ -NoExit → когда выходишь из claude, окно закрывается само (нет процесса-зомби).
    • -n <name> → понятное имя в заголовке окна и в /resume.
    """
    sid = str(uuid.uuid4())
    ps = (f"Set-Location -LiteralPath '{_ps_lit(path)}'; "
          f"& '{CLAUDE}' --session-id {sid} -n '{_ps_lit(name)[:40]}' $env:CLAUDE_TASK")
    subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        env={**os.environ, "CLAUDE_TASK": task},
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
        close_fds=True,
    )
    return sid


def resume_in_console(name: str, sid: str, path: str, prompt: str) -> None:
    """Сессия закрыта (окна нет) → открыть НОВОЕ видимое окно `claude --resume <sid>`
    в том же проекте и ВПЕЧАТАТЬ туда промт (как будто пользователь набрал и нажал
    Enter). Не headless: пользователь видит ту же сессию в консоли, как и просил.

    Окно поднимается асинхронно; как только CLI запишет свой sessions/<pid>.json с
    этим sid — впечатываем текст. Résumé переиспользует тот же sid, поэтому контекст
    цел, а окно живёт (в отличие от `-p`, которое отработало бы и закрылось)."""
    cwd = path if (path and os.path.isdir(path)) else None
    loc = f"Set-Location -LiteralPath '{_ps_lit(path)}'; " if cwd else ""
    # без -NoExit: при выходе из claude окно закроется само (нет зомби)
    ps = f"{loc}& '{CLAUDE}' --resume {sid} -n '{_ps_lit(name)[:40]}'"
    subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0), close_fds=True)

    def _worker():
        # ждём, пока откроется окно и CLI зарегистрирует сессию (pid в sessions/)
        for _ in range(60):                       # до ~60 с
            time.sleep(1)
            pid, _st = session_live_pid(sid)
            if pid:
                time.sleep(1.5)                   # дать REPL встать на чтение stdin
                terminal_inject.send_to_terminal(pid, prompt)
                return
    threading.Thread(target=_worker, daemon=True).start()


def find_session(path: str, statuses):
    """Найти сессию проекта в одном из статусов (самую свежую). Для «дописать в ждущую»."""
    try:
        snap = cu.scan()
    except Exception:
        return None
    cand = [s for s in snap["sessions"]
            if s.get("project_path") == path and s.get("status") in statuses]
    cand.sort(key=lambda s: s.get("age_sec", 1e9))
    return cand[0] if cand else None


def session_live_pid(sid: str):
    """(pid, status) живой сессии по её id, или (None, None). PID есть только у
    открытых окон — тогда можно ВПЕЧАТАТЬ ход прямо в терминал.

    Читаем МГНОВЕННО из ~/.claude/sessions/ (не гоняем полный scan на каждый Send).
    status приводим к «run/wait»: busy|shell|waiting=работает, idle=ждёт ввода."""
    if not sid:
        return None, None
    try:
        pid, cli = cu.live_pid_status(sid)
    except Exception:
        return None, None
    if not pid:
        return None, None
    status = "wait" if cli == "idle" else "run"
    return pid, status


def _submitted(sid, prompt, before_size, before_upd):
    """Признак, что ход ПРИНЯТ: ждавшая сессия ушла в работу (status≠idle или
    statusUpdatedAt вырос) — это появляется раньше записи в транскрипт; плюс запасной
    признак — ход уже виден в транскрипте."""
    st, upd = cu.session_activity(sid)
    if st and st != "idle":
        return True
    if upd and before_upd and upd > before_upd:
        return True
    return cu.user_said(sid, prompt, before_size)


def _confirm_submit(sid: str, prompt: str, before: int, before_upd, pid, aggressive: bool):
    """Убедиться, что впечатанный промт реально ОТПРАВЛЕН. Если нет — Enter «съелся»
    под лагом → жмём Enter ещё раз. aggressive=True (сессия ждала ввода) → до 3 попыток;
    иначе — одна проверка. Возврат: True / False (не подтвердилось) / None (не проверяли)."""
    rounds = 3 if aggressive else 1
    for i in range(rounds):
        deadline = time.time() + (2.5 if aggressive else 1.5)
        while time.time() < deadline:
            if _submitted(sid, prompt, before, before_upd):
                return True
            time.sleep(0.35)
        if aggressive and i < rounds - 1:
            terminal_inject.send_enter(pid)     # повторный Enter — добить отправку
    return True if _submitted(sid, prompt, before, before_upd) else (False if aggressive else None)


def inject_live(sid: str, prompt: str):
    """Впечатать ход в живой терминал сессии и подтвердить отправку.
    Возврат: (ok, (pid, msg, confirmed)). ok=None — сессия не живая (нет PID) → resume."""
    pid, status = session_live_pid(sid)
    if not (pid and status in ("run", "wait")):
        return None, None
    before = cu.transcript_size(sid)
    _st0, before_upd = cu.session_activity(sid)
    ok, msg = terminal_inject.send_to_terminal(pid, prompt)
    if not ok:
        return ok, (pid, msg, None)
    # проверяем/добиваем отправку только для настоящих промтов на ждущей сессии;
    # слэш-команды (/compact,/clear) не ложатся обычным ходом → не верифицируем.
    aggressive = (status == "wait") and not prompt.strip().startswith("/")
    confirmed = _confirm_submit(sid, prompt, before, before_upd, pid, aggressive=aggressive)
    return True, (pid, msg, confirmed)


def _claude_ps(cwd, mid: str, prompt: str, timeout: int = 1800) -> str:
    """Запустить claude через PowerShell, промт — в env-переменной (без stdin/кавычек). Вернуть текст.

    mid — доп. флаги между claude и -p (например "--resume 'uuid'") или "".
    """
    ps = (f"& '{CLAUDE}' {mid} -p $env:CLAUDE_PROMPT "
          f"--output-format text --permission-mode acceptEdits")
    env = {**os.environ, "CLAUDE_PROMPT": prompt}
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
            cwd=cwd, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (r.stdout or "").strip()
        if r.returncode != 0 and r.stderr and r.stderr.strip():
            out += "\n\n[stderr]\n" + r.stderr.strip()
        return out or "(пустой ответ)"
    except subprocess.TimeoutExpired:
        return "⏱ Таймаут (30 мин)."
    except Exception as e:
        return f"Ошибка запуска: {e}"


def run_headless_async(name: str, path: str, task: str):
    """Выполнить headless и прислать результат в TG (в фоне)."""
    tg_send(f"⚡ *{name}* — headless: запускаю…")

    def _worker():
        tg_send(f"⚡ *{name}* — headless готово:\n\n{_claude_ps(path, '', task)[:3500]}")
    threading.Thread(target=_worker, daemon=True).start()


def run_resume_async(name: str, session_id: str, path, label: str, prompt: str, use_stdin: bool = True):
    """Продолжить существующую сессию (--resume), результат в TG. label: продолжение/compact/clear."""
    tg_send(f"🔁 *{name}* — {label}: запускаю…")

    def _worker():
        out = _claude_ps(path, f"--resume '{session_id}'", prompt)
        tg_send(f"🔁 *{name}* — {label} готово:\n\n{out[:3500]}")
    threading.Thread(target=_worker, daemon=True).start()


# ---------------------------------------------------------------------------
# VPS — launch/connect a session on a remote server over SSH (config["vps"])
# The server keeps its OWN Claude auth; we never send keys, only the prompt.
# ---------------------------------------------------------------------------
def _ssh_base(tty: bool = False):
    """Common ssh args from config (key-based, non-interactive-safe)."""
    args = ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
            "-o", "ConnectTimeout=15"]
    if tty:
        args.append("-t")
    if VPS.get("key"):
        args += ["-i", VPS["key"]]
    return args


def _vps_wrap(inner: str) -> str:
    """Wrap a shell command to run as a non-root user (run_user) when set, so
    `claude` can use --dangerously-skip-permissions (blocked under root)."""
    run_user = VPS.get("run_user")
    if run_user and run_user != VPS.get("user", "root"):
        return f"sudo -u {run_user} -H bash -lc {shlex.quote(inner)}"
    return f"bash -lc {shlex.quote(inner)}"


def run_vps_headless(name: str, prompt: str, model: str = "sonnet"):
    """Run `claude -p --model <model>` on the VPS; result comes back to Telegram."""
    host = VPS.get("host"); user = VPS.get("user", "root")
    claude_r = VPS.get("claude", "/usr/bin/claude"); wdir = VPS.get("dir") or "~"
    tg_send(f"🖥 *{name}* — VPS · {model}: запускаю на {host}…")

    def _worker():
        # prompt travels base64-encoded so no quoting/locale can mangle it remotely
        b64 = base64.b64encode(prompt.encode("utf-8")).decode("ascii")
        inner = (f"cd {shlex.quote(wdir)} 2>/dev/null; "
                 f"P=$(printf %s {b64} | base64 -d); "
                 f"{shlex.quote(claude_r)} -p \"$P\" --model {model} "
                 f"--dangerously-skip-permissions --output-format text")
        cmd = _ssh_base() + [f"{user}@{host}", _vps_wrap(inner)]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True,
                               encoding="utf-8", errors="replace", timeout=1800,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            out = (r.stdout or "").strip()
            if not out and r.stderr and r.stderr.strip():
                out = "[stderr] " + r.stderr.strip()
            out = out or "(пустой ответ)"
        except subprocess.TimeoutExpired:
            out = "⏱ Таймаут (30 мин)."
        except Exception as e:
            out = f"Ошибка SSH: {e}"
        tg_send(f"🖥 *{name}* — VPS готово:\n\n{out[:3500]}")
    threading.Thread(target=_worker, daemon=True).start()


def kill_vps_sessions() -> str:
    """Kill lingering `claude` processes on the VPS (headless leftovers / closed
    terminals). Returns how many were killed, or 'err:…'."""
    host = VPS.get("host"); user = VPS.get("user", "root")
    run_user = VPS.get("run_user") or user
    # root can signal the run_user's processes directly (no sudo needed to kill)
    inner = (f"N=$(pgrep -u {shlex.quote(run_user)} -f 'claude-code/cli.js' | wc -l); "
             f"pkill -u {shlex.quote(run_user)} -f 'claude-code/cli.js' 2>/dev/null; "
             f"printf killed:%s \"$N\"")
    cmd = _ssh_base() + [f"{user}@{host}", f"bash -lc {shlex.quote(inner)}"]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (r.stdout or "").strip()
        return out.split("killed:")[-1].strip() if "killed:" in out else "?"
    except Exception as e:
        return f"err:{e}"


def start_vps_session(model: str = "sonnet") -> None:
    """Open an interactive claude session ON the VPS in a new console on this PC."""
    host = VPS.get("host"); user = VPS.get("user", "root")
    claude_r = VPS.get("claude", "/usr/bin/claude"); wdir = VPS.get("dir") or "~"
    keyarg = f"-i '{_ps_lit(VPS['key'])}' " if VPS.get("key") else ""
    inner = f"cd {shlex.quote(wdir)}; {shlex.quote(claude_r)} --model {model} --dangerously-skip-permissions"
    remote = _vps_wrap(inner).replace('"', '\\"')
    ps = f"ssh {keyarg}-t {user}@{host} \"{remote}\""
    subprocess.Popen(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0), close_fds=True,
    )


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8", cookie=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200, cookie=None):
        self._send(code, json.dumps(obj, ensure_ascii=False), cookie=cookie)

    def _is_local(self):
        """Did this request originate on the host itself (not via the tunnel)?

        Local browser: Host = loopback AND no proxy headers. Tunnelled traffic
        arrives with the public Host + nginx's X-Forwarded-* headers, so it is
        NOT treated as local (it must present Telegram auth). On the PC itself
        (127.0.0.1:<port>) control works without Telegram.
        """
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        fwd = any(self.headers.get(h) for h in
                  ("X-Forwarded-For", "X-Real-IP", "X-Forwarded-Host", "X-Forwarded-Proto"))
        return host in ("127.0.0.1", "localhost", "::1") and not fwd

    def _cookies(self):
        out = {}
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def _has_session(self):
        """Valid password cookie? (set by POST /api/login)."""
        if not WEB_PASSWORD:
            return False
        return hmac.compare_digest(self._cookies().get("cd_session", ""), _session_value())

    def _authed(self, user=None):
        """May this request CONTROL? loopback OR trusted-proxy header OR Telegram-owner OR password cookie."""
        return (self._is_local()
                or bool(WEB_TRUST_KEY and self.headers.get("X-ClaudeDock-Auth") == WEB_TRUST_KEY)
                or bool(user and OWNER_ID and user.get("id") == OWNER_ID)
                or self._has_session())

    def do_GET(self):
        path = urlparse(self.path).path
        if not path.startswith(BASE):
            self._send(404, "not found", "text/plain")
            return
        sub = path[len(BASE):].rstrip("/")
        if sub in ("", "/"):
            try:
                html = MINIAPP.read_text(encoding="utf-8")
            except OSError:
                html = "<h1>miniapp.html not found</h1>"
            self._send(200, html, "text/html; charset=utf-8")
        elif sub == "/api/usage":
            try:
                self._json(cu.scan())
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif sub == "/api/projects":
            try:
                self._json({"projects": recent_projects(),
                            "vps": {"enabled": bool(VPS.get("host")),
                                    "label": VPS.get("label") or "🖥 VPS"}})
            except Exception as e:
                self._json({"error": str(e)}, 500)
        elif sub == "/api/file":
            # Give a delivered file back so the phone can see what it sent. Guarded
            # like the rest of the page: the id is unguessable and only reachable
            # under the secret /cu/<token>/ path (same footing as /api/usage).
            fid = dict(parse_qsl(urlparse(self.path).query)).get("id", "")
            f = inbox_file(fid)
            if f is None:
                self._send(404, "not found", "text/plain")
                return
            ctype = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            try:
                self._send(200, f.read_bytes(), ctype)
            except OSError as e:
                self._send(500, str(e), "text/plain")
        elif sub == "/api/auth":
            # can THIS request control? (loopback / password cookie). Telegram is
            # checked client-side via initData. password_enabled tells the UI to
            # offer a login button.
            self._json({"control": self._authed(), "password_enabled": bool(WEB_PASSWORD)})
        else:
            self._send(404, "not found", "text/plain")

    def do_POST(self):
        path = urlparse(self.path).path
        sub = path[len(BASE):].rstrip("/") if path.startswith(BASE) else ""
        if sub not in ("/api/run", "/api/chat", "/api/seen", "/api/login", "/api/upload"):
            self._send(404, "not found", "text/plain")
            return
        if sub == "/api/upload":
            # The body is the file itself, so it cannot be parsed as JSON below.
            return self._upload()
        try:
            length = int(self.headers.get("Content-Length", "0"))
            data = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:
            self._json({"ok": False, "error": "bad request"}, 400)
            return
        if sub == "/api/login":
            pw = data.get("password") or ""
            if WEB_PASSWORD and hmac.compare_digest(pw, WEB_PASSWORD):
                cookie = (f"cd_session={_session_value()}; Path={BASE}; Max-Age=604800; "
                          "HttpOnly; SameSite=Lax")
                return self._json({"ok": True}, cookie=cookie)
            return self._json({"ok": False, "error": ("неверный пароль" if WEB_PASSWORD
                               else "пароль не настроен (web_password в config.json)")}, 403)
        user = verify_init_data(data.get("initData", ""))
        if not self._authed(user):
            self._json({"ok": False,
                        "error": "не авторизовано — войди по паролю, открой из Telegram, или прямо с ПК"}, 403)
            return
        if sub == "/api/seen":
            # пометить ответ сессии просмотренным — снимает «новый ответ» в UI
            cu.mark_seen(data.get("session_id", ""))
            return self._json({"ok": True})
        if sub == "/api/chat":
            # Синхронный чат с сессией (resume): ответ возвращается прямо в веб.
            sid = data.get("session_id", "")
            prompt = (data.get("prompt") or data.get("task") or "").strip()
            path_ = data.get("path", "")
            if not sid:
                return self._json({"ok": False, "error": "нет session_id"}, 400)
            if not prompt:
                return self._json({"ok": False, "error": "пустой промт"}, 400)
            cwd = path_ if (path_ and os.path.isdir(path_)) else None
            try:
                # Живая сессия (открытое окно) — впечатываем ход прямо в её терминал,
                # ответ идёт В ТО ЖЕ окно. Иначе — headless resume с ответом сюда.
                ok, info = inject_live(sid, prompt)
                if ok is True:
                    pid, _msg, confirmed = info
                    if confirmed is True:
                        resp = ("✅ Отправлено в консоль сессии (PID %s) — впечатано и "
                                "принято (Enter сработал). Ответ появится в том же окне." % pid)
                    elif confirmed is False:
                        resp = ("⌨ Текст впечатан в окно (PID %s), но подтверждения отправки "
                                "нет даже после повторного Enter. Глянь окно сессии — возможно, "
                                "нужно нажать Enter вручную." % pid)
                    else:
                        resp = ("⌨ Впечатано и отправлено (Enter) в консоль сессии (PID %s). "
                                "Результат появится в том же окне." % pid)
                    return self._json({"ok": True, "injected": True,
                                       "confirmed": confirmed, "response": resp})
                if ok is False:
                    pid, msg, _ = info
                    return self._json({"ok": True, "injected": False,
                        "response": "⚠ Не удалось впечатать в окно (%s). "
                                    "Окно сессии закрыто? Попробуй ещё раз." % msg})
                # окна нет — открываем видимую консоль (resume) и печатаем туда
                resume_in_console(name or "session", sid, path_, prompt)
                return self._json({"ok": True, "injected": False,
                    "response": "🖥 Сессия была закрыта — открываю её консоль заново "
                                "(claude --resume) и впечатываю сообщение в неё. Ответ "
                                "появится в самом окне сессии на ПК."})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        action = data.get("action")
        path_ = data.get("path", "")
        task = (data.get("task") or "").strip()
        name = data.get("name", "проект")
        sid = data.get("session_id", "")
        cwd = path_ if (path_ and os.path.isdir(path_)) else None
        try:
            if action in ("vps_headless", "vps_session", "vps_kill"):
                if not VPS.get("host"):
                    return self._json({"ok": False, "error": "VPS не настроен (ключ vps в config.json)"}, 400)
                if action == "vps_kill":
                    m = kill_vps_sessions()
                    if m.startswith("err:"):
                        return self._json({"ok": False, "error": "VPS: " + m[4:]}, 500)
                    return self._json({"ok": True, "msg": f"🧹 Закрыто сессий claude на VPS: {m}"})
                model = data.get("model", "sonnet")
                if model not in ("opus", "sonnet"):
                    model = "sonnet"
                if action == "vps_session":
                    start_vps_session(model)
                    return self._json({"ok": True, "msg": f"🖥 Открываю сессию на VPS ({model}) — терминал на ПК."})
                if not task:
                    return self._json({"ok": False, "error": "пустая задача"}, 400)
                run_vps_headless(VPS.get("label") or "VPS", task, model)
                return self._json({"ok": True, "msg": f"🖥 VPS ({model}) запущен — ответ придёт в Telegram."})
            if action in ("new_session", "headless"):
                if not cwd:
                    return self._json({"ok": False, "error": "проект не найден"}, 400)
                if not task:
                    return self._json({"ok": False, "error": "пустая задача"}, 400)
                if action == "new_session":
                    # Если в проекте уже есть сессия, которая ЖДЁТ ввода — дописываем
                    # задачу в неё (та же сессия), а не плодим новую (как и просил юзер).
                    waiting = find_session(cwd, ("wait",))
                    if waiting:
                        pid = waiting.get("pid")
                        if pid:
                            ok, msg = terminal_inject.send_to_terminal(pid, task)
                            if ok:
                                self._json({"ok": True, "msg": f"«{name}» ждал ввода — впечатал задачу прямо в его окно (PID {pid}). Ответ — в самом окне сессии."})
                            else:
                                self._json({"ok": False, "error": f"не удалось впечатать в окно ({msg})"}, 502)
                        else:
                            run_resume_async(name, waiting["session_id"], cwd, "продолжение", task, True)
                            self._json({"ok": True, "msg": f"«{name}» ждал ввода — дописал задачу в ту же сессию (…{waiting['session_id'][-6:]}). Ответ придёт в Telegram."})
                    else:
                        sid = start_session(cwd, task, name)
                        self._json({"ok": True, "msg": f"Новая сессия в «{name}» — окно открыто на ПК (id …{sid[-6:]})."})
                else:
                    run_headless_async(name, cwd, task)
                    self._json({"ok": True, "msg": f"Headless запущен в «{name}» — разовый агент, закроется сам, ответ придёт в Telegram."})
            elif action in ("continue", "compact", "clear"):
                if not sid:
                    return self._json({"ok": False, "error": "нет session_id"}, 400)
                if action == "continue":
                    if not task:
                        return self._json({"ok": False, "error": "пустой промт"}, 400)
                    prompt = task
                else:
                    prompt = "/compact" if action == "compact" else "/clear"
                # Живая сессия — впечатываем прямо в окно (та же сессия, ответ там же).
                ok, info = inject_live(sid, prompt)
                if ok is True:
                    pid, _msg, confirmed = info
                    if confirmed is False:
                        self._json({"ok": True, "msg": f"⌨ Впечатано в «{name}» (PID {pid}), но отправка не подтвердилась — глянь окно, возможно нужен Enter вручную."})
                    elif confirmed is True:
                        self._json({"ok": True, "msg": f"Отправлено в «{name}» (PID {pid}) — принято. Ответ в самом окне сессии."})
                    else:
                        self._json({"ok": True, "msg": f"Впечатано и отправлено (Enter) в «{name}» (PID {pid}). Результат в самом окне сессии."})
                elif ok is False:
                    pid, msg, _ = info
                    self._json({"ok": False, "error": f"не удалось впечатать в окно ({msg}) — окно закрыто?"}, 502)
                elif action == "continue":
                    resume_in_console(name, sid, cwd or path_, prompt)
                    self._json({"ok": True, "msg": f"🖥 Сессия «{name}» была закрыта — открываю её консоль заново (resume) и впечатываю сообщение. Ответ — в окне сессии на ПК."})
                else:
                    run_resume_async(name, sid, cwd, action, prompt, False)
                    self._json({"ok": True, "msg": f"Сессия закрыта — {prompt} через resume в «{name}», результат в Telegram."})
            else:
                self._json({"ok": False, "error": "неизвестное действие"}, 400)
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)

    def _upload(self):
        """Take one file from the phone and put it on this PC.

        Raw body, no multipart and no base64: the name rides in the query string
        and the Telegram signature in a header, so a 20 MB photo goes through the
        tunnel as-is instead of a third larger.
        """
        user = verify_init_data(self.headers.get("X-CD-Init", ""))
        if not self._authed(user):
            return self._json({"ok": False, "error": "не авторизовано"}, 403)
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0:
            return self._json({"ok": False, "error": "пустой файл"}, 400)
        if length > MAX_UPLOAD:
            # Do not read the body we are refusing; the socket goes with it.
            self.close_connection = True
            return self._json({"ok": False,
                               "error": "файл больше %d МБ" % (MAX_UPLOAD // 1048576)}, 413)
        chunks, got = [], 0
        while got < length:
            part = self.rfile.read(min(65536, length - got))
            if not part:
                break
            chunks.append(part)
            got += len(part)
        if got != length:
            return self._json({"ok": False, "error": "передача оборвалась"}, 400)
        name = dict(parse_qsl(urlparse(self.path).query)).get("name", "")
        try:
            info = save_attachment(b"".join(chunks), name)
        except OSError as e:
            return self._json({"ok": False, "error": str(e)}, 500)
        info["ok"] = True
        info["url"] = BASE + "/api/file?id=" + quote(info["id"])
        return self._json(info)

    def log_message(self, *a):
        pass


def main():
    args = sys.argv[1:]
    if "--selftest" in args:
        srv = ThreadingHTTPServer((HOST, PORT), Handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        import urllib.request
        u = f"http://{HOST}:{PORT}{BASE}/api/usage"
        ok_usage = b"sessions" in urllib.request.urlopen(u, timeout=40).read()
        p = f"http://{HOST}:{PORT}{BASE}/api/projects"
        ok_proj = b"projects" in urllib.request.urlopen(p, timeout=40).read()
        h = f"http://{HOST}:{PORT}{BASE}/"
        ok_html = b"Claude" in urllib.request.urlopen(h, timeout=40).read()
        # неавторизованный POST должен дать 403
        import urllib.error
        code = 0
        try:
            req = urllib.request.Request(f"http://{HOST}:{PORT}{BASE}/api/run",
                                         data=b'{"action":"new_session","path":"x","task":"y"}',
                                         headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=10)
        except urllib.error.HTTPError as e:
            code = e.code
        srv.shutdown()
        print(f"selftest: usage={ok_usage} projects={ok_proj} html={ok_html} unauth_post={code}")
        return
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"webapp on http://{HOST}:{PORT}{BASE}/  (Ctrl-C — стоп)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nстоп")


if __name__ == "__main__":
    main()
