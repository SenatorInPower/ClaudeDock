"""Local Codex usage and native queued commands. Never reads authentication files."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import config

HOME = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
DATA = Path(__file__).parent / "data"
CACHE = DATA / "codex_parse_cache.json"
_lock = threading.RLock()
_snapshot = None
_snapshot_at = 0.0
_snapshot_sig = None
_cache = None
IDLE_REBUILD_SEC = 30


def _connect(name):
    return sqlite3.connect((HOME / name).resolve().as_uri() + "?mode=ro", uri=True, timeout=2)


def executable():
    override = config.get("codex_bin")
    candidate = Path.home() / "AppData/Local/Programs/OpenAI/Codex/bin/codex.exe"
    return override or shutil.which("codex") or (str(candidate) if candidate.is_file() else "codex")


def thread_info(sid):
    if not isinstance(sid, str):
        return None
    try:
        uuid.UUID(sid)
        with _connect("state_5.sqlite") as db:
            db.row_factory = sqlite3.Row
            row = db.execute("SELECT * FROM threads WHERE id=?", (sid,)).fetchone()
            return dict(row) if row else None
    except (ValueError, OSError, sqlite3.Error):
        return None


def queue_message(sid, prompt):
    if not thread_info(sid):
        raise ValueError("Unknown Codex session")
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 24000:
        raise ValueError("Message must contain 1–24000 characters")
    # Argument vector, no shell interpolation and no second agent on the same workspace.
    result = subprocess.run(
        [executable(), "queue", "--thread", sid, "--message", prompt],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=25,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout or "Codex queue failed").strip()[:800])
    return {"ok": True, "queued": True, "provider": "codex",
            "response": "Сообщение поставлено в штатную очередь этой Codex-сессии. "
                        "Ответ появится здесь после обработки; закрытую сессию нужно возобновить в Codex."}


def _empty():
    return {"input": 0, "output": 0, "cache_write": 0, "cache_read": 0}


def _tokens(usage):
    cached = max(0, int(usage.get("cached_input_tokens", 0)))
    written = max(0, int(usage.get("cache_write_input_tokens", 0)))
    # Cached input and reasoning output are subsets, not extra tokens.
    return {"input": max(0, int(usage.get("input_tokens", 0)) - cached - written),
            "cache_read": cached, "cache_write": written,
            "output": max(0, int(usage.get("output_tokens", 0)))}


def _add(dst, src):
    for key in _empty():
        dst[key] = dst.get(key, 0) + src.get(key, 0)


def _date(ts):
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().date().isoformat()
    except (ValueError, TypeError, AttributeError):
        return None


def _load_cache():
    global _cache
    if _cache is None:
        try:
            _cache = json.loads(CACHE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            _cache = {}
    return _cache


def parse_usage(path, sid, cached=None):
    """Incremental tail. New per-response records take precedence over cumulative events."""
    path = Path(path)
    stat = path.stat()
    agg = cached or {}
    if agg.get("v") == 1 and agg.get("offset") == stat.st_size and agg.get("mtime") == stat.st_mtime:
        return agg
    if agg.get("v") != 1 or agg.get("offset", 0) > stat.st_size:
        agg = {"v": 1, "offset": 0, "records": {}, "legacy": {}, "responses": [],
               "legacy_total": -1, "has_records": False, "model": "?"}
    seen = set(agg["responses"])
    with path.open("rb") as stream:
        stream.seek(agg["offset"])
        while True:
            start = stream.tell()
            line = stream.readline()
            if not line or not line.endswith(b"\n"):
                stream.seek(start)
                break
            try:
                obj = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            payload = obj.get("payload") or {}
            kind = obj.get("type")
            if kind == "turn_context":
                agg["model"] = payload.get("model") or agg["model"]
            usage = None
            bucket = "records"
            if kind == "token_usage_record" and payload.get("thread_id", sid) == sid:
                agg["has_records"] = True
                response = payload.get("response_id")
                if response and response not in seen:
                    seen.add(response)
                    usage = payload.get("usage")
            elif kind == "event_msg" and payload.get("type") == "token_count":
                info = payload.get("info") or {}
                total = (info.get("total_token_usage") or {}).get("total_tokens")
                if total is not None and total != agg["legacy_total"]:
                    agg["legacy_total"] = total
                    usage = info.get("last_token_usage")
                    bucket = "legacy"
            day = _date(obj.get("timestamp"))
            if usage and day:
                _add(agg[bucket].setdefault(day, _empty()), _tokens(usage))
        agg["offset"] = stream.tell()
    agg["responses"] = list(seen)
    agg["mtime"] = stat.st_mtime
    agg["daily"] = agg["records"] if agg["has_records"] else agg["legacy"]
    return agg


def _live(sid):
    path = HOME / "thread-writer-locks" / (sid + ".lock")
    if not path.exists():
        return False
    try:
        with path.open("rb") as stream:
            if os.name == "nt":
                # Codex holds an exclusive byte-range writer lock on Windows.
                stream.read(1)
            else:
                import fcntl
                try:
                    fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    fcntl.flock(stream, fcntl.LOCK_UN)
                except BlockingIOError:
                    return True
    except PermissionError:
        return True
    except OSError:
        return False
    return False


def _history():
    messages, turns = {}, {}
    try:
        with _connect("thread_history_1.sqlite") as db:
            for sid, status, ordinal in db.execute(
                    "SELECT thread_id,status,rollout_ordinal FROM thread_turns ORDER BY rollout_ordinal"):
                turns[sid] = status
            for sid, raw, stamp in db.execute(
                    "SELECT thread_id,item_json,created_at_ms FROM thread_items "
                    "WHERE item_type IN ('userMessage','agentMessage') ORDER BY created_at_ms"):
                try:
                    item = json.loads(raw)
                except ValueError:
                    continue
                entry = messages.setdefault(sid, {"user": "", "assistant": "", "history": [], "count": 0})
                if item.get("type") == "userMessage":
                    value = "\n".join(p.get("text", "") for p in item.get("content", []) if p.get("type") == "text")
                    if value.strip():
                        entry["user"] = value[:4000]
                        entry["history"].append({"title": value[:300], "last_ts": datetime.fromtimestamp(stamp / 1000).astimezone().isoformat(), "tokens_h": ""})
                        entry["history"] = entry["history"][-10:]
                else:
                    entry["assistant"] = item.get("text", "")[:4000]
                    entry["answer_ts"] = datetime.fromtimestamp(stamp / 1000, timezone.utc).isoformat()
                    entry["count"] += 1
                    entry["phase"] = item.get("phase")
    except (OSError, sqlite3.Error):
        pass
    return messages, turns


def _signature():
    """Codex rewrites its sqlite (+WAL) on every turn and keeps a lock file per live thread."""
    sig = []
    for name in ("state_5.sqlite", "state_5.sqlite-wal", "thread_history_1.sqlite", "thread_history_1.sqlite-wal"):
        try:
            st = (HOME / name).stat()
            sig.append((st.st_size, st.st_mtime_ns))
        except OSError:
            sig.append(None)
    try:
        sig.append(tuple(sorted(os.listdir(HOME / "thread-writer-locks"))))
    except OSError:
        sig.append(None)
    return sig


def scan(use_cache=True, **_):
    global _snapshot, _snapshot_at, _snapshot_sig
    with _lock:
        now = time.time()
        if use_cache and _snapshot is not None and now - _snapshot_at < 3:
            return _snapshot
        # A full rebuild re-reads every rollout and the whole history db (~3 s CPU) —
        # skip it while nothing in Codex changed.
        sig = _signature()
        if use_cache and _snapshot is not None and sig == _snapshot_sig and now - _snapshot_at < IDLE_REBUILD_SEC:
            return _snapshot
        import claude_usage as cu
        seen = cu._load_json(cu.SEEN_FILE, {})
        children = {}
        try:
            with _connect("state_5.sqlite") as db:
                db.row_factory = sqlite3.Row
                threads = [dict(r) for r in db.execute("SELECT * FROM threads ORDER BY updated_at DESC")]
                for parent, child in db.execute("SELECT parent_thread_id,child_thread_id FROM thread_spawn_edges"):
                    children.setdefault(parent, []).append(child)
        except (OSError, sqlite3.Error):
            threads = []
        cache = _load_cache() if use_cache else {}
        messages, turns = _history()
        totals, today_tokens, daily, sessions = _empty(), _empty(), {}, []
        today = datetime.now().astimezone().date().isoformat()
        for thread in threads:
            sid = thread["id"]
            try:
                agg = parse_usage(thread["rollout_path"], sid, cache.get(sid))
                cache[sid] = agg
            except (OSError, ValueError, TypeError):
                agg = {"daily": {}, "model": thread.get("model") or "?"}
            tok = _empty()
            for day, amount in agg["daily"].items():
                _add(tok, amount)
                _add(daily.setdefault(day, _empty()), amount)
                if day == today:
                    _add(today_tokens, amount)
            _add(totals, tok)
            if thread.get("archived"):
                continue
            path = thread.get("cwd") or ""
            if path.startswith("\\\\?\\"):
                path = path[4:]
            history = messages.get(sid, {})
            live = _live(sid)
            status = "run" if live and turns.get(sid) == "inProgress" else "wait" if live else "old"
            updated = thread.get("updated_at") or 0
            last_ts = datetime.fromtimestamp(updated).astimezone().isoformat()
            sessions.append({
                "provider": "codex", "provider_label": "GPT / Codex", "session_id": sid,
                "project": cu.friendly_project(path), "project_path": path,
                "title": thread.get("name") or thread.get("title") or "Codex",
                "last_prompt": history.get("user", "")[:300], "last_user_text": history.get("user", ""),
                "last_response": history.get("assistant", ""), "history": history.get("history", []),
                "models": [thread.get("model") or agg.get("model") or "?"],
                "tokens": tok, "tokens_total": sum(tok.values()), "cost_usd": None,
                "msg_count": history.get("count", 0), "first_ts": datetime.fromtimestamp(thread.get("created_at") or updated).astimezone().isoformat(),
                "last_ts": last_ts, "mtime": updated, "age_sec": max(0, int(now - updated)),
                "active": live, "live": live, "pid": None, "status": status,
                "last_stop": turns.get(sid), "awaiting": None, "awaiting_detail": "",
                "unread": status == "wait" and history.get("answer_ts", "") > seen.get(sid, ""),
                "subagents": len(children.get(sid, [])), "subagent_tokens": 0, "tasks": [],
                "queue_supported": True, "agent_path": thread.get("agent_path") or "",
            })
        if use_cache:
            DATA.mkdir(exist_ok=True)
            tmp = CACHE.with_suffix(f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, CACHE)
        for amount in daily.values():
            amount["tokens"] = sum(amount.values())
        sessions.sort(key=lambda s: ({"run": 0, "wait": 1, "old": 2}[s["status"]], s["age_sec"]))
        _snapshot = {"generated_at": datetime.now().astimezone().isoformat(), "now": now,
                     "sessions": sessions, "session_count": len(sessions),
                     "active_count": sum(s["active"] for s in sessions), "live_count": sum(s["live"] for s in sessions),
                     "attention_count": sum(s["unread"] for s in sessions), "agents_available": bool(threads),
                     "totals": {"tokens": totals, "tokens_total": sum(totals.values()), "cost_usd": None},
                     "today": {"date": today, "tokens": today_tokens, "tokens_total": sum(today_tokens.values()), "cost_usd": None},
                     "daily": daily, "avg7_tokens": 0, "avg30_tokens": 0, "active_window_sec": 180,
                     "cost_note": "Local token usage. Subscription charges cannot be inferred from these counters."}
        _snapshot_at = time.time()
        _snapshot_sig = sig
        for window in (7, 30):
            cutoff = (datetime.now().astimezone().date() - timedelta(days=window - 1)).isoformat()
            _snapshot[f"avg{window}_tokens"] = sum(v["tokens"] for d, v in daily.items() if d >= cutoff) / window
        return _snapshot
