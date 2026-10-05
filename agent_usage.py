"""Provider-aware facade; Claude and Codex remain separately inspectable."""
import threading
import time

import claude_usage as claude
import codex_usage as codex

_lock = threading.RLock()
_snapshot = None
_at = 0


def __getattr__(name):
    return getattr(claude, name)


def human_cost(value):
    return "—" if value is None else claude.human_cost(value)


def scan(use_cache=True, use_agents=True):
    global _snapshot, _at
    with _lock:
        if use_cache and _snapshot is not None and time.time() - _at < 3:
            return _snapshot
        cs = claude.scan(use_cache=use_cache, use_agents=use_agents)
        gs = codex.scan(use_cache=use_cache)
        for session in cs["sessions"]:
            session["provider"] = "claude"
            session["provider_label"] = "Claude"
        combined = dict(cs)
        sessions = cs["sessions"] + gs["sessions"]
        sessions.sort(key=lambda s: ({"run": 0, "wait": 1, "old": 2}.get(s["status"], 3), s["age_sec"]))
        combined["sessions"] = sessions
        combined["session_count"] = len(sessions)
        for key in ("active_count", "live_count", "attention_count"):
            combined[key] = cs[key] + gs[key]
        for key in ("today", "totals"):
            value = dict(cs[key])
            value["tokens"] = {k: cs[key]["tokens"].get(k, 0) + gs[key]["tokens"].get(k, 0) for k in cs[key]["tokens"]}
            value["tokens_total"] = cs[key]["tokens_total"] + gs[key]["tokens_total"]
            value["cost_usd"] = None if gs[key]["tokens_total"] else cs[key]["cost_usd"]
            combined[key] = value
        combined["daily"] = {day: dict(value) for day, value in cs["daily"].items()}
        for day, value in gs["daily"].items():
            target = combined["daily"].setdefault(day, {})
            for key, count in value.items():
                target[key] = target.get(key, 0) + count
        combined["providers"] = {"claude": cs, "codex": gs}
        for window in (7, 30):
            key = f"avg{window}_tokens"
            combined[key] = cs[key] + gs[key]
        _snapshot, _at = combined, time.time()
        return combined
