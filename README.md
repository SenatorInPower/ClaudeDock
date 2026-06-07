<div align="center">

# ✳️ ClaudeDock

### See every [Claude Code](https://claude.com/claude-code) session at a glance — and never miss the one that needs you.

A tiny always‑on‑top **desktop dock** + **web cockpit** for all your Claude Code sessions.
Who's working, who's waiting on you, who just replied — one look. Costs **0 extra tokens**.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.9+](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://www.python.org/)
[![Claude tokens: 0](https://img.shields.io/badge/Claude%20tokens-0-success)](#-cost)
[![PRs welcome](https://img.shields.io/badge/PRs-welcome-brightgreen.svg)](#-contributing)
[![Stars](https://img.shields.io/github/stars/SenatorInPower/ClaudeDock?style=social)](https://github.com/SenatorInPower/ClaudeDock/stargazers)

</div>

> **The problem:** you run 5–10 Claude Code sessions at once — one per repo, per terminal, per worktree — and you constantly alt‑tab to figure out *"is this one working, finished, or stuck waiting for my answer?"*
>
> **ClaudeDock** answers that without you looking for it: a column of avatars on your screen edge, one per live session, colour‑coded and badged. Read the last reply or write back into any session from your **desktop, browser, or phone** — never hunting through windows again.

<div align="center">

<a href="media/claudedock-demo.mp4">
  <img src="media/claudedock-demo.gif" alt="ClaudeDock demo — desktop dock, session table, web cockpit" width="900">
</a>

<sub>20‑sec tour — desktop dock → session table → web cockpit (abstract demo data). ▶︎ <a href="media/claudedock-demo.mp4">Watch the full video (with sound)</a>.</sub>

</div>

---

## Table of contents

- [✨ Features](#-features)
- [⚡ Quick start](#-quick-start)
- [⚙️ Configuration](#️-configuration)
- [🖥️ The three surfaces](#️-the-three-surfaces)
- [🌐 Remote access (with or without Telegram)](#-remote-access-with-or-without-telegram)
- [🔒 Security](#-security)
- [💸 Cost](#-cost)
- [🧠 How it works](#-how-it-works)
- [❓ FAQ](#-faq)
- [🗺️ Roadmap](#️-roadmap)
- [🤝 Contributing](#-contributing)

## ✨ Features

- 🟧🟦 **Working vs waiting, at a glance.** Orange ring = the agent is working; blue ring = it's waiting on you. Working sessions float to the top.
- ❓ **"Needs your decision" detection.** It spots when a session is blocked on a multiple‑choice question (`AskUserQuestion`), a permission prompt, or ended its turn with a question — and badges it ❓/⚠.
- 🔴 **Never miss a reply.** A red dot marks sessions that answered while you were away. Click to read the last response; the dot clears.
- ↩️ **Jump back in from anywhere.** Write a follow‑up into the *same* session (full context preserved) from desktop, browser, or phone.
- 📊 **Exact token & cost accounting.** Per‑model input/output/cache tokens and $ cost, today / 7‑day / all‑time — summed from local transcripts.
- 🧩 **Subagents folded in.** Task‑tool subagents show as a badge on their parent, not as noise.
- 🪙 **0 extra tokens.** It reads `~/.claude` transcripts + the `claude agents` CLI. It never calls the model.
- 🔧 **Config‑driven & self‑hosted.** No accounts, no telemetry, no cloud. Telegram and remote web access are optional add‑ons.

## ⚡ Quick start

```bash
git clone https://github.com/SenatorInPower/ClaudeDock.git
cd ClaudeDock
pip install -r requirements.txt

# Desktop dock (always-on-top, right edge):
python dock.py --right          # or double-click dock.bat on Windows

# Web cockpit (this machine) — open the URL it prints:
python webapp.py                # http://127.0.0.1:8765/cu/<secret>/
```

That's it — with **zero config** the dock already shows your sessions. Requirements: **Python 3.9+**, [Claude Code](https://claude.com/claude-code) on your PATH, and `pip install -r requirements.txt` (`Pillow` for the dock, `requests` for Telegram — the web UI and core are stdlib‑only).

## ⚙️ Configuration

Copy `config.example.json` → `config.json` (git‑ignored) and set only what you need. Any key also works as an env var `CLAUDEDOCK_<KEY>`.

<details>
<summary><b>Add your projects</b> — friendly names + the launch picker</summary>

```jsonc
{
  // How sessions are LABELLED — matched as a lowercased substring of the cwd:
  "project_names": [
    ["myorg/backend", "🛠 Backend"],
    ["myorg/web-app", "🌐 Web App"]
  ],
  // What appears in the "launch a session" picker (web / Telegram / dock right-click):
  "launch_projects": [
    { "name": "Backend", "path": "C:/code/myorg/backend" },
    { "name": "Web App", "path": "C:/code/myorg/web-app" }
  ]
}
```
`C:\code\myorg\backend` → labelled **🛠 Backend**; unmatched paths fall back to the folder name. Edits apply on the next refresh — no restart.
</details>

<details>
<summary><b>Remote control without Telegram</b> — a password login</summary>

```jsonc
{ "web_password": "choose-a-strong-one" }
```
Set it, restart `webapp.py`, and the web UI shows a **🔓 Log in** button. After login you control sessions remotely over HTTPS with no Telegram at all. (On the local PC you never need it — `127.0.0.1` is always trusted.)
</details>

<details>
<summary><b>Telegram</b> — reports, alerts, Mini App (optional)</summary>

Create a bot with [@BotFather](https://t.me/BotFather), then:
```jsonc
{ "telegram_bot_token": "123:ABC...", "telegram_chat_id": "<your id>" }
```
You get usage‑spike alerts (`alerts.py`), one‑command reports (`python claude_usage.py --send`), and a Telegram‑authenticated Mini App. Leave empty to keep Telegram fully off.
</details>

## 🖥️ The three surfaces

| Surface | File | What it's for |
|---|---|---|
| 🪟 **Desktop dock** | `dock.py` | Avatars on your screen edge. Hover = last reply + history; click = full window; right‑click = write / open folder / new session. |
| 🌐 **Web cockpit** | `webapp.py` + `miniapp.html` | The same data + controls in a browser or on your phone. |
| 💬 **Telegram** | `tg.py`, `alerts.py` | Push reports & spike alerts; the web cockpit doubles as a Telegram Mini App. |

## 🌐 Remote access (with or without Telegram)

ClaudeDock watches the sessions **on the machine it runs on**, so it's self‑hosted, one instance per person. To reach *your* instance from elsewhere:

1. **Local only (default).** Web UI on `127.0.0.1:8765` — full control, no auth needed.
2. **Password login (no Telegram).** Set `web_password`, expose the port over HTTPS (e.g. a reverse SSH tunnel → your server with nginx + TLS), and log in with your password. Want a second gate? Stack **HTTP Basic Auth** in front:
   ```nginx
   location /cu/ {
       auth_basic "ClaudeDock";
       auth_basic_user_file /etc/nginx/.htpasswd;   # htpasswd -c ... yourname
       proxy_pass http://127.0.0.1:9099;            # the tunnel endpoint
   }
   ```
   → that gives you **TLS + a secret URL token + a browser login + the app password** — four layers, no Telegram.
3. **Telegram Mini App.** Control locked to your Telegram user id via signed `initData`. See `run_webapp.example.ps1`.

> 🔑 Rule of thumb: **viewing** can be shared; **control** stays with you (loopback, password, or Telegram). Want a teammate to drive *their* agents? They run their own ClaudeDock.

## 🔒 Security

- **Local‑first.** Reads only `~/.claude` and shells out to `claude agents`. Nothing leaves your machine unless *you* enable Telegram or the tunnel.
- **Control is gated.** A secret token in the URL path **plus** one of: a loopback request, a valid password cookie, or a Telegram `initData` signature matching the owner. Tunnelled traffic can't pretend to be local.
- **No secrets in the repo.** All keys live in `config.json` / env vars (git‑ignored). The repo ships only placeholders.

## 💸 Cost

**Zero Claude tokens to monitor.** ClaudeDock never calls the model to watch your sessions — it reads local transcripts and the local `claude agents` command. Watching 1 or 50 sessions is free. *(Writing back into a session — "continue"/"headless" — runs `claude`, which costs tokens like any prompt.)*

## 🧠 How it works

```
~/.claude/projects/**/*.jsonl  ──parse──▶  claude_usage.scan()  ──▶  { sessions, tokens, status, last_reply, awaiting, ... }
        (local transcripts)                       ▲
   `claude agents --json`  ──live status──────────┘   (busy / waiting / idle)
                                                   │
                ┌──────────────────────────────────┼──────────────────────────────┐
                ▼                                   ▼                               ▼
            dock.py                            webapp.py                      tg.py / alerts.py
       desktop dock                       web + Telegram Mini App           reports & alerts
```

Liveness is taken from `claude agents --json` (authoritative); the transcript only enriches it. The core (`claude_usage.py`) is dependency‑free — add a field to `scan()` and it flows to all three surfaces.

## 🧩 Project layout

```
claude_usage.py   core: parse transcripts + live status -> snapshot (also a CLI)
dock.py           desktop dock (Tkinter + Pillow)
webapp.py         stdlib HTTP server: web UI + control API
miniapp.html      the web / Telegram Mini App front-end
tg.py             tiny Telegram push (optional)
alerts.py         background usage alerts (optional)
config.py         config loader (env > config.json > defaults)
```

## ❓ FAQ

<details><summary>Does it use my Claude tokens?</summary>
No. Monitoring reads local files + the local <code>claude agents</code> CLI. Only writing back into a session runs the model.
</details>

<details><summary>Do I need Telegram?</summary>
No. The desktop dock and the local web UI work with zero config. Telegram is just an optional remote/notification add‑on — and you can now control remotely with a <code>web_password</code> instead.
</details>

<details><summary>Which platforms?</summary>
The dock is Windows‑first (Tkinter + Pillow). The core and the web cockpit are cross‑platform — macOS/Linux dock is on the roadmap.
</details>

<details><summary>Can my teammates use it?</summary>
Each person runs their own instance (it watches the sessions on its host). Viewing can be shared read‑only; control stays with the owner.
</details>

## 🗺️ Roadmap

- [ ] Read‑only **demo mode** (sample data) for public links
- [ ] Full **English / i18n** pass on the UI strings
- [ ] Desktop **notifications** when a session starts waiting on you
- [ ] **macOS / Linux** dock

## 🤝 Contributing

Issues and PRs welcome! The core is small and dependency‑free — new `scan()` fields automatically reach every surface.

<div align="center">

### ⭐ If ClaudeDock saves you a tab‑hunt, give it a star — it helps a lot.

**[MIT](LICENSE)** · made by [Vadim Khavronen (lnPower)](https://github.com/SenatorInPower)

</div>
