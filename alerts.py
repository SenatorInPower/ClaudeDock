# -*- coding: utf-8 -*-
"""Claude Usage — фоновые алармы в Telegram при высоком расходе.

Консервативно и без спама: дедуп по дню (каждое условие срабатывает раз в день),
высокие пороги (в норме молчит, реагирует только на всплеск). Пороги правь ниже.

    python alerts.py            # цикл (проверка каждые 5 мин)
    python alerts.py --once     # одна проверка (с отправкой)
    python alerts.py --dry      # показать текущие значения vs пороги, без отправки
"""
import json
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import claude_usage as cu
from tg import send

DATA = Path(__file__).parent / "data"
STATE = DATA / "alerts_state.json"
CHECK_SEC = 300

# ---- Пороги (правь под себя) ----
DAY_TOKENS = 2_000_000_000     # алерт, если за сегодня суммарно больше токенов
ACTIVE_AGENTS = 6              # алерт, если активных агентов не меньше


def _load():
    try:
        return json.loads(STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError):
        return {}


def _save(s):
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        STATE.write_text(json.dumps(s, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def check(send_msg=True):
    snap = cu.scan(use_cache=True)
    st = _load()
    today = date.today().isoformat()
    if st.get("day") != today:
        st = {"day": today, "fired": []}   # новый день — сброс дедупа
    fired = set(st.get("fired", []))
    msgs = []

    tt = snap["today"]["tokens_total"]
    if tt > DAY_TOKENS and "day_total" not in fired:
        msgs.append(f"📈 Сегодня уже {cu.human_tokens(tt)} токенов "
                    f"(≈{cu.human_cost(snap['today']['cost_usd'])}).")
        fired.add("day_total")

    ac = snap["active_count"]
    if ac >= ACTIVE_AGENTS and "many_active" not in fired:
        names = ", ".join(cu.friendly_project(s["project_path"]) for s in snap["sessions"] if s["active"])
        msgs.append(f"🤖 Активно {ac} агентов: {names[:200]}")
        fired.add("many_active")

    st["fired"] = sorted(fired)
    _save(st)
    if msgs and send_msg:
        send("⚠️ *Claude Usage — алерт*\n" + "\n".join(msgs) + "\n\n_Пороги: alerts.py_")
    return msgs


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if "--dry" in sys.argv:
        snap = cu.scan()
        tt = snap["today"]["tokens_total"]
        print(f"today {cu.human_tokens(tt)} > {DAY_TOKENS/1e9:.1f}B ? {tt > DAY_TOKENS}")
        print(f"active {snap['active_count']} >= {ACTIVE_AGENTS} ? {snap['active_count'] >= ACTIVE_AGENTS}")
        print("=> сейчас сработало бы:", check(send_msg=False))
        return
    if "--once" in sys.argv:
        print("fired:", check())
        return
    while True:
        try:
            check()
        except Exception as e:
            print("alert error:", e, file=sys.stderr)
        time.sleep(CHECK_SEC)


if __name__ == "__main__":
    main()
