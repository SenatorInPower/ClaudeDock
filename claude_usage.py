# -*- coding: utf-8 -*-
"""Claude Usage Monitor — ядро.

Читает локальные транскрипты Claude Code (~/.claude/projects/**/*.jsonl) и считает
расход токенов по сессиям/задачам/моделям/дням. НЕ обращается к API Claude — это
чистый парсинг локальных файлов, поэтому стоит 0 токенов.

Оптимизация: транскрипты append-only, поэтому для активных (растущих) файлов
дочитываются только новые байты (tail), а результат парсинга кэшируется на диске.
Обновление раз в несколько секунд почти бесплатно по CPU/IO.

CLI:
    python claude_usage.py --summary     # таблица в консоль
    python claude_usage.py --json        # сырой снапшот (JSON)
    python claude_usage.py --tg          # текст сводки для Telegram (печать)
    python claude_usage.py --send        # собрать сводку и отправить в TG (через tg.py)
    python claude_usage.py --watch       # live-таблица в консоли (обновление каждые 3с)

Импорт:
    from claude_usage import scan, format_tg_report
    snap = scan()
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone, date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import config

# ----------------------------------------------------------------------------
# Конфигурация
# ----------------------------------------------------------------------------

PROJECTS_DIR = Path.home() / ".claude" / "projects"
DATA_DIR = Path(__file__).parent / "data"
CACHE_FILE = DATA_DIR / "parse_cache.json"
DAILY_FILE = DATA_DIR / "daily_totals.json"
# Версия схемы кэша парсинга. Меняй при изменении формы agg, чтобы старый
# parse_cache.json не отдавал записи без новых полей (last_assistant_stop и т.п.).
CACHE_VERSION = 3

# Сессия считается «активной» (агент работает прямо сейчас), если её транскрипт
# писался не позже чем ACTIVE_SECONDS назад.
ACTIVE_SECONDS = 180
# Статусы сессии (для контроллера агентов):
#   run  — агент сейчас работает (файл пишется / последний ход = tool_use);
#   wait — агент ответил (end_turn) и ждёт следующего сообщения пользователя;
#   old  — давно не трогали, скорее всего окно закрыто.
RUN_SECONDS = 12                 # файл рос только что -> агент печатает прямо сейчас
WAIT_KEEP_HOURS = 12             # в пределах этого end_turn-сессия считается «ждёт»
HISTORY_MAX = 10                 # сколько последних команд показывать на сессию
# Сколько дней динамики хранить/показывать.
DAILY_KEEP_DAYS = 60

# Цены за 1M токенов. Источник: справочник claude-api (кэш 2026-05-26).
# Кортеж: (input, output, cache_write_5m, cache_write_1h, cache_read).
# Правь здесь, если цены изменятся.
PRICING = {
    "opus":   (5.0, 25.0, 6.25, 10.0, 0.5),
    "sonnet": (3.0, 15.0, 3.75, 6.0, 0.3),
    "haiku":  (1.0, 5.0, 1.25, 2.0, 0.1),
}


def price_key(model_id: str) -> str:
    m = (model_id or "").lower()
    if "haiku" in m:
        return "haiku"
    if "sonnet" in m:
        return "sonnet"
    return "opus"  # opus и неизвестные считаем по opus


# Friendly display names per cwd, matched as a lowercased substring of the
# session's working directory. Loaded from config — add your own in config.json
# ("project_names": [["fragment", "Display Name"], ...]).
_PROJECT_NAMES = config.project_names()


def friendly_project(cwd: str) -> str:
    if not cwd:
        return "📁 (unknown)"
    norm = cwd.replace("\\", "/").lower()
    for frag, name in _PROJECT_NAMES:
        if frag in norm:
            return name
    base = cwd.replace("\\", "/").rstrip("/").split("/")[-1]
    return "📁 " + (base or cwd)


def pretty_model(model_id: str) -> str:
    if not model_id:
        return "?"
    s = model_id.replace("claude-", "")
    s = re.sub(r"-\d{8}$", "", s)            # убрать дату-суффикс
    s = re.sub(r"(\d+)-(\d+)", r"\1.\2", s)  # 4-8 -> 4.8
    return s


# ----------------------------------------------------------------------------
# Утилиты
# ----------------------------------------------------------------------------

def human_tokens(n: float) -> str:
    n = float(n or 0)
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return f"{int(n)}"


def human_cost(usd: float) -> str:
    usd = float(usd or 0)
    if usd >= 100:
        return f"${usd:.0f}"
    if usd >= 1:
        return f"${usd:.2f}"
    return f"${usd:.3f}"


def _parse_ts(ts: str):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _local_date(ts: str) -> str | None:
    dt = _parse_ts(ts)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().date().isoformat()


def _empty_tok():
    return {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}


def _add_tok(dst: dict, src: dict):
    for k in ("input", "output", "cache_write", "cache_read"):
        dst[k] = dst.get(k, 0) + src.get(k, 0)


def tok_total(t: dict) -> int:
    return int(t.get("input", 0) + t.get("output", 0)
               + t.get("cache_write", 0) + t.get("cache_read", 0))


def cost_of(model_id: str, t: dict, cw5: int = 0, cw1h: int = 0) -> float:
    """Стоимость токенов одной модели. cw5/cw1h — разбивка cache_write по TTL."""
    pin, pout, pcw5, pcw1h, pcr = PRICING[price_key(model_id)]
    cw = t.get("cache_write", 0)
    if cw5 == 0 and cw1h == 0:
        cw5 = cw  # без разбивки считаем как 5m
    return (t.get("input", 0) * pin
            + t.get("output", 0) * pout
            + cw5 * pcw5
            + cw1h * pcw1h
            + t.get("cache_read", 0) * pcr) / 1_000_000.0


# ----------------------------------------------------------------------------
# Парсинг одного транскрипта (с инкрементальным дочитыванием)
# ----------------------------------------------------------------------------

def _blank_agg():
    return {
        "by_model": {},        # model_id -> {input,output,cache_write,cache_read, cw5, cw1h}
        "by_date": {},         # 'YYYY-MM-DD' -> {input,output,cache_write,cache_read}
        "msg_count": 0,
        "first_ts": None,
        "last_ts": None,
        "cwd": "",
        "session_id": "",
        "git_branch": "",
        "version": "",
        "ai_title": "",
        "last_prompt": "",
        "last_assistant_stop": None,  # stop_reason последнего ответа: tool_use|end_turn|...
        "last_assistant_text": "",    # текст последнего ответа агента (показать по клику)
        "last_assistant_ts": None,    # время последнего ответа агента
        "pending_tools": None,        # висящий tool_use без результата: {ids,names,ask}
        "last_kind": None,            # тип последней значимой записи
        "tasks": [],           # [{title, tokens, first_ts, last_ts}]
    }


def _is_real_user_prompt(obj: dict) -> bool:
    """Отличить настоящий запрос пользователя от tool_result (тоже role=user)."""
    if obj.get("type") != "user":
        return False
    content = (obj.get("message") or {}).get("content")
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        for b in content:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                return False
        return True
    return False


def _first_line(text: str, limit: int = 80) -> str:
    if not text:
        return ""
    line = text.strip().splitlines()[0] if text.strip() else ""
    line = line.strip()
    return line[:limit] + ("…" if len(line) > limit else "")


# Служебные «реплики» (не команды пользователя) — отфильтровываем из истории.
_NOISE_PREFIXES = (
    "<task-notification", "<local-command", "<system-reminder", "<bash-input",
    "<bash-stdout", "<bash-stderr", "<command-message", "<command-stdout",
    "<command-args", "<command-contents", "caveat:", "[request interrupted",
    "continue from where you left off", "<<autonomous",
    "this session is being continued", "your tool call was malformed",
    "please continue", "[the user has",
)


def _human_command(title: str):
    """Привести запись задачи к «команде, которую дал пользователь» или None (мусор)."""
    t = (title or "").strip()
    if not t:
        return None
    # слэш-команда: <command-name>/compact</command-name> -> /compact
    m = re.search(r"<command-name>\s*(/[\w:.-]+)", t)
    if m:
        return m.group(1)
    low = t.lower()
    if low.startswith(_NOISE_PREFIXES):
        return None
    if t.startswith("<"):           # прочие служебные xml-теги
        return None
    return t


def _process_line(line: str, agg: dict):
    """Разобрать одну JSONL-строку и влить в агрегат agg."""
    line = line.strip()
    if not line:
        return
    try:
        obj = json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return

    typ = obj.get("type")

    # Метаданные (могут быть на user/assistant строках)
    for fld, key in (("cwd", "cwd"), ("sessionId", "session_id"),
                     ("gitBranch", "git_branch"), ("version", "version")):
        v = obj.get(fld)
        if v and not agg.get(key):
            agg[key] = v

    # tool_result в user-записи закрывает «висящий» tool_use: инструмент отработал,
    # значит агент НЕ ждёт подтверждения/ответа от пользователя.
    if typ == "user":
        ucontent = (obj.get("message") or {}).get("content")
        if isinstance(ucontent, list):
            pend = agg.get("pending_tools")
            if pend:
                for b in ucontent:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        tid = b.get("tool_use_id")
                        if tid in pend.get("ids", []):
                            pend["ids"] = [x for x in pend["ids"] if x != tid]
                if not pend.get("ids"):
                    agg["pending_tools"] = None

    if typ == "ai-title":
        t = obj.get("aiTitle")
        if t:
            agg["ai_title"] = t
            # пометить текущую задачу этим заголовком, если она безымянная
            if agg["tasks"] and not agg["tasks"][-1].get("titled"):
                agg["tasks"][-1]["title"] = t
                agg["tasks"][-1]["titled"] = True
        return

    if typ == "last-prompt":
        lp = obj.get("lastPrompt")
        if lp:
            agg["last_prompt"] = lp
        return

    if _is_real_user_prompt(obj):
        # начать новую задачу
        content = (obj.get("message") or {}).get("content")
        if isinstance(content, list):
            text = " ".join(b.get("text", "") for b in content
                            if isinstance(b, dict) and b.get("type") == "text")
        else:
            text = content or ""
        ts = obj.get("timestamp")
        agg["last_kind"] = "user_prompt"
        agg["pending_tools"] = None   # новый запрос пользователя снимает «висящий» вопрос
        agg["tasks"].append({
            "title": _first_line(text) or "(задача)",
            "user_text": _first_line(text, 110),   # сырой текст запроса (для истории)
            "titled": False,
            "tokens": 0,
            "first_ts": ts,
            "last_ts": ts,
        })
        return

    if typ == "assistant":
        amsg = obj.get("message") or {}
        stop = amsg.get("stop_reason")
        if stop:
            agg["last_assistant_stop"] = stop
        agg["last_kind"] = "assistant"
        # последний ответ агента: текст (для показа) + висящие tool_use (вопрос/действия)
        agg["last_assistant_ts"] = obj.get("timestamp")
        a_content = amsg.get("content")
        a_texts, a_tool_ids, a_tool_names, a_ask = [], [], [], None
        if isinstance(a_content, list):
            for b in a_content:
                if not isinstance(b, dict):
                    continue
                bt = b.get("type")
                if bt == "text":
                    a_texts.append(b.get("text", ""))
                elif bt == "tool_use":
                    a_tool_names.append(b.get("name"))
                    if b.get("id"):
                        a_tool_ids.append(b.get("id"))
                    if b.get("name") == "AskUserQuestion":
                        a_ask = b.get("input")
        elif isinstance(a_content, str):
            a_texts.append(a_content)
        a_joined = "\n".join(t for t in a_texts if t and t.strip())
        if a_joined.strip():
            agg["last_assistant_text"] = a_joined[:2000]
        if a_tool_ids:
            agg["pending_tools"] = {"ids": a_tool_ids, "names": a_tool_names, "ask": a_ask}
        elif stop == "end_turn":
            agg["pending_tools"] = None
        usage = amsg.get("usage")
        if not isinstance(usage, dict):
            return
        model = amsg.get("model") or "unknown"
        inp = int(usage.get("input_tokens", 0) or 0)
        out = int(usage.get("output_tokens", 0) or 0)
        cw = int(usage.get("cache_creation_input_tokens", 0) or 0)
        cr = int(usage.get("cache_read_input_tokens", 0) or 0)
        cc = usage.get("cache_creation") or {}
        cw5 = int(cc.get("ephemeral_5m_input_tokens", 0) or 0)
        cw1h = int(cc.get("ephemeral_1h_input_tokens", 0) or 0)
        if cw5 == 0 and cw1h == 0:
            cw5 = cw

        bm = agg["by_model"].setdefault(
            model, {"input": 0, "output": 0, "cache_write": 0,
                    "cache_read": 0, "cw5": 0, "cw1h": 0})
        bm["input"] += inp
        bm["output"] += out
        bm["cache_write"] += cw
        bm["cache_read"] += cr
        bm["cw5"] += cw5
        bm["cw1h"] += cw1h

        ts = obj.get("timestamp")
        d = _local_date(ts)
        if d:
            bd = agg["by_date"].setdefault(d, _empty_tok())
            bd["input"] += inp
            bd["output"] += out
            bd["cache_write"] += cw
            bd["cache_read"] += cr

        # атрибуция задаче (токены = input+output+cache, как «вес» работы)
        weight = inp + out + cw + cr
        if not agg["tasks"]:
            agg["tasks"].append({"title": agg.get("ai_title") or "(задача)",
                                 "titled": bool(agg.get("ai_title")),
                                 "tokens": 0, "first_ts": ts, "last_ts": ts})
        agg["tasks"][-1]["tokens"] += weight
        if ts:
            agg["tasks"][-1]["last_ts"] = ts

        agg["msg_count"] += 1
        if ts:
            if not agg["first_ts"]:
                agg["first_ts"] = ts
            agg["last_ts"] = ts


def parse_file(path: Path, cached: dict | None) -> dict:
    """Вернуть {size, parsed_bytes, mtime, agg} для файла, дочитывая хвост если можно."""
    st = path.stat()
    size, mtime = st.st_size, st.st_mtime

    if cached and cached.get("size") == size and cached.get("mtime") == mtime:
        return cached  # ничего не изменилось

    # Решаем: дочитать хвост или перечитать целиком
    if cached and size >= cached.get("size", 0) and cached.get("parsed_bytes", 0) <= size:
        agg = cached["agg"]
        start = cached.get("parsed_bytes", 0)
    else:
        agg = _blank_agg()
        start = 0

    parsed_bytes = start
    try:
        with open(path, "rb") as f:
            f.seek(start)
            data = f.read()
    except OSError:
        return cached or {"size": size, "parsed_bytes": 0, "mtime": mtime, "agg": _blank_agg()}

    # Обрабатываем только полные строки (до последнего \n); хвост без \n оставим на потом
    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        complete = b""
    else:
        complete = data[:last_nl + 1]
        parsed_bytes = start + last_nl + 1

    if complete:
        text = complete.decode("utf-8", errors="replace")
        for line in text.split("\n"):
            if line:
                _process_line(line, agg)

    return {"size": size, "parsed_bytes": parsed_bytes, "mtime": mtime, "agg": agg}


# ----------------------------------------------------------------------------
# Кэш на диске
# ----------------------------------------------------------------------------

def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return default


def _save_json(path: Path, obj):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


# ----------------------------------------------------------------------------
# Живые сессии — источник правды о статусе (`claude agents --json`)
# ----------------------------------------------------------------------------

# Path to the `claude` CLI for the live-sessions query. On Windows shutil.which
# resolves the .BAT/.cmd wrapper, which subprocess runs directly. Override via
# config ("claude_bin") if claude is not on PATH.
_CLAUDE_BIN = config.claude_bin()

# Кэш списка живых агентов: вызов claude стоит ~1.5с и плодит процесс, поэтому
# держим результат ttl секунд. fetched=True кэширует и неудачу (не долбим claude).
_agents_cache = {"ts": 0.0, "data": None, "fetched": False}


def live_agents(ttl: float = 5.0):
    """Карта живых интерактивных сессий Claude Code или None.

    `claude agents --json` перечисляет процессы с ОТКРЫТЫМ окном (kind=interactive)
    и состоянием: busy=агент работает, idle=ждёт ввода. Это единственный надёжный
    признак «терминал ещё открыт» — по транскрипту этого не понять (ответ end_turn
    час назад выглядит одинаково и у открытого окна, и у закрытого).

    Возвращает {session_id: {status, pid, cwd, kind, startedAt}} либо None, если
    claude недоступен/команда упала (тогда статус оценивается по транскрипту).
    """
    now = time.time()
    c = _agents_cache
    if c["fetched"] and (now - c["ts"]) < ttl:
        return c["data"]
    result = None
    try:
        r = subprocess.run(
            [_CLAUDE_BIN, "agents", "--json"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=8, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode == 0 and (r.stdout or "").strip():
            data = json.loads(r.stdout)
            mp = {}
            if isinstance(data, list):
                for a in data:
                    sid = a.get("sessionId")
                    if sid:
                        mp[sid] = {"status": (a.get("status") or "").lower(),
                                   "pid": a.get("pid"), "cwd": a.get("cwd") or "",
                                   "kind": a.get("kind") or "",
                                   "startedAt": a.get("startedAt")}
            result = mp
    except (OSError, ValueError, subprocess.SubprocessError):
        result = None
    if result is None:
        # `claude agents` не ответил (таймаут/сбой) — переиспользуем последний
        # успешный снимок (≤30с), чтобы счётчик живых сессий не «прыгал» из-за
        # разовых сбоев команды (это и есть «то много агентов, то резко мало»).
        lg, lg_ts = c.get("last_good"), c.get("last_good_ts", 0.0)
        if lg is not None and (now - lg_ts) < 30:
            result = lg
    else:
        c["last_good"], c["last_good_ts"] = result, now
    c["ts"], c["data"], c["fetched"] = now, result, True
    return result


# ----------------------------------------------------------------------------
# «Бот ждёт твоего решения» + отметка «просмотрено»
# ----------------------------------------------------------------------------

SEEN_FILE = DATA_DIR / "seen.json"


def _ends_with_question(text: str) -> bool:
    """Последняя непустая строка ответа — вопрос (агент спросил «что дальше»)."""
    t = (text or "").rstrip()
    if not t:
        return False
    last = t.splitlines()[-1].rstrip()
    return last.endswith(("?", "?)", "?»", "?»"))


def _trailing_question(text: str, limit: int = 180) -> str:
    t = (text or "").rstrip()
    if not t:
        return ""
    last = t.splitlines()[-1].strip()
    return last[:limit]


def mark_seen(sid: str, ts: str | None = None):
    """Отметить ответ сессии как просмотренный — снимает флаг «непрочитано»."""
    if not sid:
        return
    seen = _load_json(SEEN_FILE, {})
    seen[sid] = ts or datetime.now(timezone.utc).isoformat()
    try:
        _save_json(SEEN_FILE, seen)
    except OSError:
        pass


# ----------------------------------------------------------------------------
# Полное сканирование -> снапшот
# ----------------------------------------------------------------------------

def scan(use_cache: bool = True, use_agents: bool = True) -> dict:
    now = time.time()
    # Живые сессии (правда о статусе): {sid: {...}} или None, если claude недоступен.
    agents = live_agents() if use_agents else None
    seen = _load_json(SEEN_FILE, {})   # отметки «просмотрено» для флага «непрочитано»
    cache = _load_json(CACHE_FILE, {}) if use_cache else {}
    # схема агрегата меняется — сбрасываем кэш при несовпадении версии
    if cache.get("__v") != CACHE_VERSION:
        cache = {}
    new_cache = {"__v": CACHE_VERSION}

    # Сгруппировать файлы по сессии (subagents -> родитель)
    files = []
    if PROJECTS_DIR.exists():
        files = list(PROJECTS_DIR.rglob("*.jsonl"))

    parsed = {}  # path_str -> parse result
    for p in files:
        key = str(p)
        try:
            res = parse_file(p, cache.get(key))
        except OSError:
            continue
        new_cache[key] = res
        parsed[key] = res

    if use_cache:
        try:
            _save_json(CACHE_FILE, new_cache)
        except OSError:
            pass

    # Сборка сессий. session_id берём из agg; subagent-файлы лежат в
    # <session_uuid>/subagents/agent-*.jsonl — родитель = имя папки.
    sessions = {}  # sid -> dict

    def _get_session(sid, path):
        if sid not in sessions:
            sessions[sid] = {
                "session_id": sid,
                "project": "", "project_path": "",
                "title": "", "last_prompt": "",
                "by_model": {}, "by_date": {},
                "msg_count": 0, "first_ts": None, "last_ts": None,
                "mtime": 0, "subagents": 0, "subagent_tokens": 0,
                "last_assistant_stop": None,
                "last_assistant_text": "", "last_assistant_ts": None,
                "pending_tools": None,
                "tasks": [],
            }
        return sessions[sid]

    for key, res in parsed.items():
        p = Path(key)
        agg = res["agg"]
        is_sub = "subagents" in p.parts
        # определить sid
        if is_sub:
            # родительская папка сессии: .../<uuid>/subagents/agent-x.jsonl
            try:
                sid = p.parts[p.parts.index("subagents") - 1]
            except (ValueError, IndexError):
                sid = agg.get("session_id") or p.stem
        else:
            sid = agg.get("session_id") or p.stem

        s = _get_session(sid, p)
        # слить by_model
        for model, bm in agg["by_model"].items():
            d = s["by_model"].setdefault(
                model, {"input": 0, "output": 0, "cache_write": 0,
                        "cache_read": 0, "cw5": 0, "cw1h": 0})
            for k in d:
                d[k] += bm.get(k, 0)
        # слить by_date
        for dt, t in agg["by_date"].items():
            bd = s["by_date"].setdefault(dt, _empty_tok())
            _add_tok(bd, t)
        s["msg_count"] += agg["msg_count"]
        s["mtime"] = max(s["mtime"], res["mtime"])
        for fld in ("first_ts", "last_ts"):
            v = agg.get(fld)
            if v:
                if not s[fld] or (fld == "first_ts" and v < s[fld]) or (fld == "last_ts" and v > s[fld]):
                    s[fld] = v
        if is_sub:
            s["subagents"] += 1
            for bm in agg["by_model"].values():
                s["subagent_tokens"] += bm["input"] + bm["output"] + bm["cache_write"] + bm["cache_read"]
        else:
            if agg.get("cwd"):
                s["project_path"] = agg["cwd"]
                s["project"] = friendly_project(agg["cwd"])
            if agg.get("ai_title"):
                s["title"] = agg["ai_title"]
            if agg.get("last_prompt"):
                s["last_prompt"] = agg["last_prompt"]
            if agg.get("last_assistant_stop"):
                s["last_assistant_stop"] = agg["last_assistant_stop"]
            if agg.get("last_assistant_text"):
                s["last_assistant_text"] = agg["last_assistant_text"]
            if agg.get("last_assistant_ts"):
                s["last_assistant_ts"] = agg["last_assistant_ts"]
            s["pending_tools"] = agg.get("pending_tools")
            # задачи (только из основного транскрипта)
            for tk in agg.get("tasks", []):
                s["tasks"].append({"title": tk.get("title", "(задача)"),
                                   "user_text": tk.get("user_text") or tk.get("title", ""),
                                   "tokens": tk.get("tokens", 0),
                                   "last_ts": tk.get("last_ts")})

    # Финализация сессий
    out_sessions = []
    grand = _empty_tok()
    grand_cost = 0.0
    today = date.today().isoformat()
    today_tok = _empty_tok()
    today_cost = 0.0
    daily_calc = {}  # date -> tokens total

    for sid, s in sessions.items():
        tok = _empty_tok()
        cost = 0.0
        models = []
        for model, bm in s["by_model"].items():
            models.append(model)
            mt = {"input": bm["input"], "output": bm["output"],
                  "cache_write": bm["cache_write"], "cache_read": bm["cache_read"]}
            _add_tok(tok, mt)
            cost += cost_of(model, mt, bm.get("cw5", 0), bm.get("cw1h", 0))
        _add_tok(grand, tok)
        grand_cost += cost

        # дневные суммы
        for dt, t in s["by_date"].items():
            db = daily_calc.setdefault(dt, _empty_tok())
            _add_tok(db, t)
        # сегодня по сессии
        if today in s["by_date"]:
            _add_tok(today_tok, s["by_date"][today])

        age = int(now - s["mtime"]) if s["mtime"] else 999999
        stop = s.get("last_assistant_stop")

        # Статус для контроллера: run=работает(жёлтый) / wait=ждёт(синий) / old=закрыта.
        # Правда о том, открыт ли терминал, — из `claude agents --json` (agents):
        # сессии НЕТ в списке => окно закрыто (old), каким бы свежим ни был транскрипт.
        # Это убирает старый баг, когда любой транскрипт за 12ч красился в «ждёт».
        ag = agents.get(sid) if agents is not None else None
        live_pid = ag.get("pid") if ag else None
        if agents is not None:
            if ag is None:
                live, status = False, "old"
            elif ag.get("status") == "idle":
                live, status = True, "wait"
            elif ag.get("status") == "busy":
                live, status = True, "run"
            elif ag.get("status") == "waiting":
                # waiting = агент ВЫПОЛНЯЕТ инструмент (напр. долгий PowerShell/Bash)
                # или обрабатывает — это работа, а не ожидание твоего ввода.
                live, status = True, "run"
            else:  # неизвестный статус живой сессии — уточняем по транскрипту
                live = True
                status = "run" if (stop == "tool_use" or age <= RUN_SECONDS) else "wait"
        else:
            # Фолбэк: claude agents недоступен — эвристика по транскрипту (как раньше).
            if age <= RUN_SECONDS or (stop != "end_turn" and age <= ACTIVE_SECONDS):
                status = "run"
            elif age <= WAIT_KEEP_HOURS * 3600:
                status = "wait"
            else:
                status = "old"
            live = status in ("run", "wait")
        active = live

        # Что агент ждёт ИМЕННО ОТ ТЕБЯ — только для живых idle (wait):
        #   question   — задал вопрос с выбором пунктов (AskUserQuestion);
        #   permission — висит подтверждение действий (tool_use без результата);
        #   ask        — закончил ход вопросом «что дальше».
        # busy/закрытые сюда не попадают: у них tool_use = просто работа.
        awaiting = None
        awaiting_detail = None
        last_text = s.get("last_assistant_text") or ""
        if status == "wait":
            pend = s.get("pending_tools")
            if pend and pend.get("ids"):
                if pend.get("ask"):
                    awaiting = "question"
                    qs = (pend.get("ask") or {}).get("questions") or []
                    awaiting_detail = [
                        {"q": q.get("question", ""),
                         "options": [o.get("label", "") for o in (q.get("options") or [])]}
                        for q in qs]
                else:
                    awaiting = "permission"
                    awaiting_detail = pend.get("names") or []
            elif s.get("last_assistant_stop") == "end_turn" and _ends_with_question(last_text):
                awaiting = "ask"
                awaiting_detail = _trailing_question(last_text)

        # «есть свежий ответ» (непрочитано): живая idle-сессия, чей ответ новее
        # отметки seen (снимается кликом по агенту в UI через mark_seen).
        ans_ts = s.get("last_assistant_ts") or ""
        unread = bool(status == "wait" and ans_ts and ans_ts > seen.get(sid, ""))

        # лучший заголовок задачи
        title = s["title"] or _first_line(s["last_prompt"]) or (
            s["tasks"][-1]["title"] if s["tasks"] else "(нет задачи)")

        # топ задачи по токенам
        top_tasks = sorted(s["tasks"], key=lambda x: x["tokens"], reverse=True)[:5]

        # история команд (хронологически, последние HISTORY_MAX, только реальные
        # запросы пользователя — без служебных тегов; подряд-дубли схлопываем)
        history = []
        for t in s["tasks"]:
            cmd = _human_command(t.get("user_text") or t.get("title"))
            if not cmd:
                continue
            if history and history[-1]["title"] == cmd:
                continue
            history.append({"title": cmd, "last_ts": t.get("last_ts"),
                            "tokens_h": human_tokens(t.get("tokens", 0))})
        history = history[-HISTORY_MAX:]

        out_sessions.append({
            "session_id": sid,
            "project": s["project"] or friendly_project(s["project_path"]),
            "project_path": s["project_path"],
            "title": title,
            "last_prompt": s["last_prompt"],
            "models": [pretty_model(m) for m in models] or ["?"],
            "tokens": tok,
            "tokens_total": tok_total(tok),
            "cost_usd": round(cost, 4),
            "msg_count": s["msg_count"],
            "first_ts": s["first_ts"],
            "last_ts": s["last_ts"],
            "mtime": s["mtime"],
            "age_sec": age,
            "active": active,
            "live": live,
            "pid": live_pid,
            "status": status,
            "last_stop": stop,
            "awaiting": awaiting,              # question|permission|ask|None — ждёт твоего решения
            "awaiting_detail": awaiting_detail,
            "last_response": last_text[:1500],  # последний ответ агента (показать по клику)
            "unread": unread,                  # есть свежий ответ, ещё не просмотрен
            "subagents": s["subagents"],
            "subagent_tokens": s["subagent_tokens"],
            "tasks": [{"title": t["title"], "tokens": t["tokens"],
                       "tokens_h": human_tokens(t["tokens"])} for t in top_tasks],
            "history": history,
        })

    # Стоимость за сегодня: by_date хранится без разбивки по модели, поэтому
    # оцениваем по средней цене сессии за токен (cost сессии × доля сегодняшних токенов).
    today_cost = _estimate_today_cost(sessions, today)

    # Сортировка: работающие → ждущие → закрытые, внутри по свежести
    _pri = {"run": 0, "wait": 1, "old": 2}
    out_sessions.sort(key=lambda x: (_pri.get(x["status"], 3), x["age_sec"]))

    # Слияние дневной истории с персистентной БД (сохраняет историю,
    # даже если старые транскрипты удалят).
    daily_db = _load_json(DAILY_FILE, {}) if use_cache else {}
    for dt, t in daily_calc.items():
        total = tok_total(t)
        prev = daily_db.get(dt, {})
        # берём максимум — пересчёт даёт полную сумму за день
        if total >= prev.get("tokens", 0):
            daily_db[dt] = {"tokens": total,
                            "input": t["input"], "output": t["output"],
                            "cache_write": t["cache_write"], "cache_read": t["cache_read"]}
    # обрезать старое
    cutoff = (date.today() - timedelta(days=DAILY_KEEP_DAYS)).isoformat()
    daily_db = {d: v for d, v in daily_db.items() if d >= cutoff}
    if use_cache:
        try:
            _save_json(DAILY_FILE, daily_db)
        except OSError:
            pass

    # средние
    days_sorted = sorted(daily_db.keys())
    last7 = days_sorted[-7:]
    last30 = days_sorted[-30:]
    avg7 = (sum(daily_db[d]["tokens"] for d in last7) / len(last7)) if last7 else 0
    avg30 = (sum(daily_db[d]["tokens"] for d in last30) / len(last30)) if last30 else 0

    active_count = sum(1 for s in out_sessions if s["active"])
    live_count = sum(1 for s in out_sessions if s.get("live"))
    attention_count = sum(1 for s in out_sessions if s.get("awaiting") or s.get("unread"))
    today_total = tok_total(today_tok)

    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "now": now,
        "projects_dir": str(PROJECTS_DIR),
        "sessions": out_sessions,
        "active_count": active_count,
        "live_count": live_count,
        "attention_count": attention_count,
        "agents_available": agents is not None,
        "session_count": len(out_sessions),
        "totals": {"tokens": grand, "tokens_total": tok_total(grand),
                   "cost_usd": round(grand_cost, 2)},
        "today": {"date": today, "tokens": today_tok, "tokens_total": today_total,
                  "cost_usd": round(today_cost, 2)},
        "daily": daily_db,
        "avg7_tokens": avg7,
        "avg30_tokens": avg30,
        "active_window_sec": ACTIVE_SECONDS,
    }


def _estimate_today_cost(sessions: dict, today: str) -> float:
    """Оценка стоимости за сегодня: цена сессии за токен * сегодняшние токены сессии."""
    total = 0.0
    for s in sessions.values():
        if today not in s["by_date"]:
            continue
        # стоимость всей сессии и её токены
        sess_cost = 0.0
        sess_tok = 0
        for model, bm in s["by_model"].items():
            mt = {"input": bm["input"], "output": bm["output"],
                  "cache_write": bm["cache_write"], "cache_read": bm["cache_read"]}
            sess_cost += cost_of(model, mt, bm.get("cw5", 0), bm.get("cw1h", 0))
            sess_tok += tok_total(mt)
        if sess_tok <= 0:
            continue
        today_tok = tok_total(s["by_date"][today])
        total += sess_cost * (today_tok / sess_tok)
    return total


# ----------------------------------------------------------------------------
# Процессы (грубо, для сводки в TG)
# ----------------------------------------------------------------------------

_proc_cache = {"ts": 0.0, "data": {"claude": 0, "node": 0}}


def running_processes() -> dict:
    """Грубая оценка числа процессов Claude/Node (Windows tasklist).

    Кэш на 30с + жёсткий таймаут 2.5с — tasklist на загруженном ПК медленный,
    раньше он подвешивал сборку отчёта.
    """
    now = time.time()
    if now - _proc_cache["ts"] < 30 and _proc_cache["ts"] > 0:
        return _proc_cache["data"]
    out = {"claude": 0, "node": 0}
    try:
        import subprocess
        r = subprocess.run(["tasklist", "/fo", "csv", "/nh"],
                           capture_output=True, text=True, timeout=2.5)
        for line in r.stdout.splitlines():
            low = line.lower()
            if '"claude' in low:
                out["claude"] += 1
            elif '"node.exe"' in low:
                out["node"] += 1
    except Exception:
        out = _proc_cache["data"]  # фолбэк на прошлое значение
    _proc_cache.update({"ts": now, "data": out})
    return out


# ----------------------------------------------------------------------------
# Форматирование сводки для Telegram
# ----------------------------------------------------------------------------

def _spark(values, width=14):
    blocks = "▁▂▃▄▅▆▇█"
    if not values:
        return ""
    vals = values[-width:]
    mx = max(vals) or 1
    return "".join(blocks[min(len(blocks) - 1, int(v / mx * (len(blocks) - 1)))] for v in vals)


def format_tg_report(snap: dict | None = None) -> str:
    if snap is None:
        snap = scan()
    lines = []
    lines.append("📊 *Claude Usage — сводка*")
    lines.append(f"_{snap['generated_at']}_")
    lines.append("")

    # Активные агенты
    active = [s for s in snap["sessions"] if s["active"]]
    lines.append(f"🤖 *Активных агентов:* {snap['active_count']}")
    if active:
        for s in active[:8]:
            sub = f" +{s['subagents']}🧩" if s["subagents"] else ""
            mins = s["age_sec"] // 60
            age = "только что" if s["age_sec"] < 45 else f"{mins} мин назад"
            lines.append(f"  ● {s['project']}{sub} — _{s['title'][:60]}_")
            lines.append(f"     {human_tokens(s['tokens_total'])} tok · "
                         f"≈{human_cost(s['cost_usd'])} · {'/'.join(s['models'][:2])} · {age}")
    else:
        lines.append("  _нет активных (никто не пишет в транскрипт прямо сейчас)_")

    # Процессы
    proc = running_processes()
    lines.append("")
    lines.append(f"🖥 *Процессы:* claude≈{proc['claude']}, node≈{proc['node']} "
                 f"(точная привязка к сессиям недоступна)")

    # Сегодня
    t = snap["today"]
    lines.append("")
    lines.append(f"📅 *Сегодня ({t['date']}):* {human_tokens(t['tokens_total'])} tok · "
                 f"≈{human_cost(t['cost_usd'])}")
    tk = t["tokens"]
    lines.append(f"   in {human_tokens(tk['input'])} · out {human_tokens(tk['output'])} · "
                 f"cache wr {human_tokens(tk['cache_write'])} / rd {human_tokens(tk['cache_read'])}")

    # Динамика
    daily = snap["daily"]
    if daily:
        days = sorted(daily.keys())[-14:]
        vals = [daily[d]["tokens"] for d in days]
        lines.append("")
        lines.append(f"📈 *Динамика (14 дн):* {_spark(vals)}")
        lines.append(f"   ср/день: 7д {human_tokens(snap['avg7_tokens'])} · "
                     f"30д {human_tokens(snap['avg30_tokens'])} tok")

    # Всего
    tot = snap["totals"]
    lines.append("")
    lines.append(f"Σ *Всего (доступные транскрипты):* {human_tokens(tot['tokens_total'])} tok · "
                 f"≈{human_cost(tot['cost_usd'])} · сессий: {snap['session_count']}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# Консольная таблица
# ----------------------------------------------------------------------------

def print_summary(snap: dict | None = None):
    if snap is None:
        snap = scan()
    print(f"CLAUDE USAGE  ·  {snap['generated_at']}  ·  "
          f"активно {snap['active_count']}/{snap['session_count']}")
    print("-" * 78)
    print(f"{'':1} {'PROJECT':22} {'TASK':26} {'TOKENS':>8} {'COST':>8} {'AGE':>6}")
    for s in snap["sessions"][:25]:
        mark = "●" if s["active"] else "○"
        age = s["age_sec"]
        age_s = f"{age}s" if age < 90 else (f"{age // 60}m" if age < 5400 else f"{age // 3600}h")
        print(f"{mark} {s['project'][:22]:22} {s['title'][:26]:26} "
              f"{human_tokens(s['tokens_total']):>8} {human_cost(s['cost_usd']):>8} {age_s:>6}")
    print("-" * 78)
    t = snap["today"]
    print(f"СЕГОДНЯ: {human_tokens(t['tokens_total'])} tok ≈{human_cost(t['cost_usd'])}   |   "
          f"7д ср: {human_tokens(snap['avg7_tokens'])}/день   |   "
          f"Σ {human_tokens(snap['totals']['tokens_total'])} ≈{human_cost(snap['totals']['cost_usd'])}")


def _watch():
    try:
        while True:
            snap = scan()
            os.system("cls" if os.name == "nt" else "clear")
            print_summary(snap)
            print("\n(обновление каждые 3с · Ctrl-C выход)")
            time.sleep(3)
    except KeyboardInterrupt:
        print("\nстоп")


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main(argv):
    try:  # консоль Windows по умолчанию cp1252 — переключаем на utf-8
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    arg = argv[1] if len(argv) > 1 else "--summary"
    if arg == "--json":
        print(json.dumps(scan(), ensure_ascii=False, indent=2))
    elif arg == "--tg":
        print(format_tg_report())
    elif arg == "--send":
        sys.path.insert(0, str(Path(__file__).parent))
        from tg import send
        ok = send(format_tg_report())
        print("sent" if ok else "failed")
    elif arg == "--watch":
        _watch()
    else:
        print_summary()


if __name__ == "__main__":
    main(sys.argv)
