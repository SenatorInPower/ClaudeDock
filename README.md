# ClaudeDock

**A desktop dock + web cockpit for all your [Claude Code](https://claude.com/claude-code) sessions.**
See who's working 🟧 vs waiting 🟦 at a glance, jump into any session, and get pinged
when an agent needs your decision — all by reading local transcripts, so it costs
**0 extra Claude tokens**.

![status](https://img.shields.io/badge/status-active-brightgreen) ![python](https://img.shields.io/badge/python-3.9%2B-blue) ![license](https://img.shields.io/badge/license-MIT-black)

> If you run several Claude Code sessions at once (one per project / repo / terminal),
> you lose track of which one is busy, which one is blocked on a question, and how many
> tokens they're burning. ClaudeDock puts a tiny always‑on‑top dock on the edge of your
> screen — one "head" per live session — and lets you read the last reply or write back
> into any of them without hunting through terminal windows.

---

## Why

- **One glance, all sessions.** A column of avatars on your screen edge — orange ring = working, blue ring = waiting for you. Working sessions float to the top.
- **Know when an agent needs YOU.** It detects when a session is blocked on a multiple‑choice question (`AskUserQuestion`), a permission prompt, or ended its turn with a question — and badges it ❓/⚠. No more "is it stuck or just done?".
- **Never miss a reply.** A red dot marks sessions that answered while you were away. Click to read the last response; the dot clears.
- **Jump in from anywhere.** Write a follow‑up into the *same* session (context preserved) from the desktop, a web page, or Telegram — without switching windows.
- **Free.** It parses `~/.claude/projects/**/*.jsonl` locally and asks the `claude` CLI for live status. It never calls the model, so it adds **zero tokens / zero cost**.

## How it works

```
~/.claude/projects/**/*.jsonl  ──parse──▶  claude_usage.scan()  ──▶  { sessions, tokens, status, ... }
        (local transcripts)                       ▲
   `claude agents --json`  ──live status──────────┘   (busy / idle / waiting)
                                                   │
                ┌──────────────────────────────────┼──────────────────────────────┐
                ▼                                   ▼                               ▼
            dock.py                            webapp.py                      tg.py / alerts.py
       desktop dock (Tkinter)           web + Telegram Mini App            optional TG reports
```

- **Liveness is authoritative.** Status comes from `claude agents --json` (`busy` = generating, `waiting` = running a tool, `idle` = waiting for your input). A session not in that list = its terminal is closed. The transcript only enriches it (last reply, pending question).
- **Token accounting is exact and free.** Per‑model input/output/cache tokens and cost are summed from the transcripts and cached incrementally on disk.

## Requirements

- **Python 3.9+**
- [Claude Code](https://claude.com/claude-code) installed (the `claude` CLI on your PATH)
- `pip install -r requirements.txt` — `Pillow` (desktop dock) and `requests` (Telegram). The web UI and the core need only the standard library.

## Install & run

```bash
git clone https://github.com/<you>/ClaudeDock.git
cd ClaudeDock
pip install -r requirements.txt

# 1) Desktop dock (always-on-top, right edge):
python dock.py --right        # or: pythonw dock.py --right   (no console window)

# 2) Web cockpit (this machine):
python webapp.py              # open the http://127.0.0.1:8765/cu/<secret>/ URL it prints
```

With **no configuration at all** the dock works fully — it just shows your sessions with
folder‑name labels. Everything below is optional polish.

## Configuration

Copy `config.example.json` to `config.json` (git‑ignored) and set only what you need.
Every key can also be supplied as an environment variable `CLAUDEDOCK_<KEY>` (see `.env.example`).

```jsonc
{
  // Friendly names in the dock/web — matched as a lowercased SUBSTRING of a session's cwd:
  "project_names": [
    ["myorg/backend", "🛠 Backend"],
    ["myorg/web-app", "🌐 Web App"]
  ],
  // Projects offered in the "launch a session" picker:
  "launch_projects": [
    { "name": "Backend", "path": "C:/code/myorg/backend" },
    { "name": "Web App", "path": "C:/code/myorg/web-app" }
  ],

  "telegram_bot_token": "",   // optional — reports, alerts, Mini-App auth
  "telegram_chat_id":  "",    // optional — where to send them
  "web_url_secret":    "",    // optional — auto-generated on first run
  "web_host": "127.0.0.1",
  "web_port": 8765,
  "claude_bin": ""            // only if `claude` isn't on your PATH
}
```

### Adding your projects (the "launch / projects" section)

Two independent lists:

1. **`project_names`** — how a session is *labelled*. ClaudeDock lowercases each session's
   working directory and shows the name of the first fragment that matches.
   `["myorg/backend", "🛠 Backend"]` turns `C:\code\myorg\backend` into **🛠 Backend**.
   Anything unmatched falls back to the folder name.
2. **`launch_projects`** — what appears in the **"launch a session"** picker in the
   web/Telegram UI and the dock's right‑click → *New session in project*. Each entry is
   `{ "name": "...", "path": "..." }`; picking one starts `claude` in that folder
   (with a pre‑generated `--session-id`, so ClaudeDock can keep talking to it afterwards).

Edit `config.json` and the lists update on the next refresh — no restart of the core needed.

### Telegram (optional)

Create a bot with [@BotFather](https://t.me/BotFather), put the token + your chat id in
`config.json`, and you get: usage‑spike **alerts** (`alerts.py`), one‑command **reports**
(`python claude_usage.py --send`), and a **Telegram Mini App** for the web UI (authenticated
by Telegram — see below). With no token, all of this is simply off.

## The three surfaces

| Surface | File | What it's for |
|---|---|---|
| **Desktop dock** | `dock.py` | Always‑on‑top avatars on your screen edge. Hover = last reply + history; click = full window; right‑click = write into the session / open folder / new session. |
| **Web cockpit** | `webapp.py` + `miniapp.html` | The same data + controls in a browser / on your phone. Status colors, "needs‑you" badges, last reply, launch & write‑back. |
| **Telegram** | `tg.py`, `alerts.py` | Push reports and spike alerts; the web cockpit doubles as a Telegram Mini App. |

## Giving other people web access

ClaudeDock watches the Claude Code sessions **on the machine it runs on**, so the natural
model is **self‑hosted, one instance per person** — the repo itself is how you "give people
the tool". For remote access there are three patterns:

1. **Local only (default).** The web UI binds `127.0.0.1:8765`. Best for a single user on one box.
2. **Your own sessions, remotely (recommended).** Keep it bound to localhost and expose it
   through a **reverse SSH tunnel → your server (nginx + TLS) → a Telegram Mini App**. Control
   is locked to *your* Telegram user id (HMAC over the Mini App `initData`), and the URL carries
   a secret path token. See `run_webapp.example.ps1`. This is how the author drives it from a phone.
3. **A shared / read‑only demo.** Because control is owner‑locked, a shared instance is only
   safe **read‑only** (viewing status & tokens, no launching). A "demo mode" that serves sample
   data is on the roadmap — ideal for a public link or a portfolio screenshot.

> Rule of thumb: **view** can be shared; **control** stays with the machine owner. To let a
> teammate drive *their* agents, they run their own ClaudeDock.

## Security model

- **Local first.** The core only reads files under `~/.claude` and shells out to `claude agents`. Nothing leaves your machine unless you enable Telegram or the tunnel.
- **Web control is double‑gated.** A secret token in the URL path **and** either a loopback request *or* a valid Telegram `initData` signature whose user id equals the owner. Tunnelled traffic (which carries proxy headers) must present Telegram auth — it can't pretend to be local.
- **No secrets in the repo.** All keys live in `config.json` / env vars, which are git‑ignored. `config.example.json` ships only placeholders.

## Cost

**Zero Claude tokens for monitoring.** ClaudeDock never calls the model to watch your
sessions — it reads local transcripts and the local `claude agents` command, so watching 1 or
50 sessions costs nothing. (Writing *back* into a session via "continue" / "headless" does run
`claude`, which naturally costs tokens like any prompt.)

## Project layout

```
claude_usage.py   core: parse transcripts + live status -> snapshot (also a CLI)
dock.py           desktop dock (Tkinter + Pillow)
webapp.py         stdlib HTTP server: web UI + control API
miniapp.html      the web / Telegram Mini App front-end
tg.py             tiny Telegram push (optional)
alerts.py         background usage alerts (optional)
config.py         config loader (env > config.json > defaults)
config.example.json / .env.example   templates
```

## CLI

```bash
python claude_usage.py --summary   # table in the console
python claude_usage.py --watch     # live table (refreshes every 3s)
python claude_usage.py --json      # raw snapshot (JSON)
python claude_usage.py --send      # build a summary and send it to Telegram
```

## Roadmap

- Read‑only **demo mode** (sample data) for public links.
- Full **English / i18n** pass on the UI strings.
- Per‑session desktop notifications when `awaiting` flips on.
- macOS/Linux dock (currently Windows‑first; the core + web are cross‑platform).

## Contributing

Issues and PRs welcome. The core (`claude_usage.py`) is dependency‑free and easy to extend —
new fields added to `scan()` automatically flow to all three surfaces.

## License

[MIT](LICENSE) © 2026 Vadim Khavronen (lnPower)
