# -*- coding: utf-8 -*-
"""Claude Usage Monitor — веб-приложение (live-вью + управление).

PC-сервер (stdlib). Отдаёт:
  GET  /cu/<token>/                 — страница Mini App (miniapp.html)
  GET  /cu/<token>/api/usage        — live данные (scan, 0 токенов Claude)
  GET  /cu/<token>/api/projects     — недавние проекты для запуска
  POST /cu/<token>/api/run          — управление: новая сессия / headless
                                      (требует подписи Telegram initData = владелец)

Снаружи доступен через reverse-туннель PC:8765 -> VPS, nginx проксирует /cu/.
Управление защищено двойным фактором: секретный токен в пути + проверка
Telegram WebApp initData (HMAC по токену бота, user.id == владелец).

    python webapp.py            # 127.0.0.1:8765
    python webapp.py --selftest
"""
import hashlib
import hmac
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

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
                                             # header X-ClaudeDock-Auth: <key> to vouch for the request -> control


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
    открытых окон (claude agents) — тогда можно ВПЕЧАТАТЬ ход прямо в терминал."""
    if not sid:
        return None, None
    try:
        snap = cu.scan()
    except Exception:
        return None, None
    for s in snap["sessions"]:
        if s.get("session_id") == sid:
            return s.get("pid"), s.get("status")
    return None, None


def inject_live(sid: str, prompt: str):
    """Попробовать впечатать ход в живой терминал сессии. (ok, message|None).
    ok=None — сессия не живая (нет PID), нужно идти через resume."""
    pid, status = session_live_pid(sid)
    if not (pid and status in ("run", "wait")):
        return None, None
    ok, msg = terminal_inject.send_to_terminal(pid, prompt)
    return ok, (pid, msg)


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
            encoding="utf-8", errors="replace", timeout=timeout)
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
                self._json({"projects": recent_projects()})
            except Exception as e:
                self._json({"error": str(e)}, 500)
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
        if sub not in ("/api/run", "/api/chat", "/api/seen", "/api/login"):
            self._send(404, "not found", "text/plain")
            return
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
                    pid, _ = info
                    return self._json({"ok": True, "injected": True,
                        "response": ("✅ Впечатано в живой терминал сессии (PID %s). "
                                     "Ответ появится в самом окне сессии — это та же "
                                     "сессия, контекст сохранён." % pid)})
                if ok is False:
                    pid, msg = info
                    return self._json({"ok": True, "injected": False,
                        "response": "⚠ Не удалось впечатать в окно (%s). "
                                    "Окно сессии закрыто? Попробуй ещё раз." % msg})
                out = _claude_ps(cwd, f"--resume '{sid}'", prompt, timeout=110)
                return self._json({"ok": True, "response": out})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 500)
        action = data.get("action")
        path_ = data.get("path", "")
        task = (data.get("task") or "").strip()
        name = data.get("name", "проект")
        sid = data.get("session_id", "")
        cwd = path_ if (path_ and os.path.isdir(path_)) else None
        try:
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
                    pid, _ = info
                    self._json({"ok": True, "msg": f"Впечатано в живой терминал «{name}» (PID {pid}) — ответ в самом окне сессии."})
                elif ok is False:
                    pid, msg = info
                    self._json({"ok": False, "error": f"не удалось впечатать в окно ({msg}) — окно закрыто?"}, 502)
                elif action == "continue":
                    run_resume_async(name, sid, cwd, "продолжение", prompt, True)
                    self._json({"ok": True, "msg": f"Сессия закрыта — отправил через resume в «{name}», ответ придёт в Telegram."})
                else:
                    run_resume_async(name, sid, cwd, action, prompt, False)
                    self._json({"ok": True, "msg": f"Сессия закрыта — {prompt} через resume в «{name}», результат в Telegram."})
            else:
                self._json({"ok": False, "error": "неизвестное действие"}, 400)
        except Exception as e:
            self._json({"ok": False, "error": str(e)}, 500)

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
