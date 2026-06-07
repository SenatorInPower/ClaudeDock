# -*- coding: utf-8 -*-
"""Tiny Telegram push (optional). No-op when no bot token is configured.

    from tg import send
    send("*Hi*")            # Markdown by default; falls back to plain on parse error
    python tg.py "text"     # from argument or stdin

Configure a bot token + chat id in config.json (or CLAUDEDOCK_TELEGRAM_* env vars)
to enable reports/alerts. Without them ClaudeDock works fine — TG just stays off.
"""
import sys
import requests

from config import BOT_TOKEN as TOKEN, CHAT_ID

API = f"https://api.telegram.org/bot{TOKEN}" if TOKEN else ""


def send(text: str, parse_mode: str = "Markdown") -> bool:
    """Send text (split into 4000-char chunks). False if TG is disabled or failed."""
    if not (TOKEN and CHAT_ID):
        return False
    ok = True
    for i in range(0, len(text), 4000):
        chunk = text[i:i + 4000]
        payload = {"chat_id": CHAT_ID, "text": chunk, "disable_web_page_preview": True}
        if parse_mode:
            payload["parse_mode"] = parse_mode
        try:
            r = requests.post(f"{API}/sendMessage", json=payload, timeout=20)
            if not r.json().get("ok"):
                # Markdown may fail to parse on user text — retry as plain.
                requests.post(f"{API}/sendMessage",
                              json={"chat_id": CHAT_ID, "text": chunk}, timeout=20)
        except Exception as e:
            print(f"TG send error: {e}", file=sys.stderr)
            ok = False
    return ok


if __name__ == "__main__":
    msg = " ".join(sys.argv[1:]) if len(sys.argv) > 1 else sys.stdin.read()
    print("sent" if send(msg) else "failed (telegram not configured?)")
