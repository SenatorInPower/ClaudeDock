# -*- coding: utf-8 -*-
"""
sleep_sync — отложенный спящий режим, синхронизированный с агентами Claude Code.

Задача: «усыпить ПК, когда все агенты закончат работу». Спящий режим откладывается,
пока хоть один агент реально работает, и срабатывает, как только все затихли.

Что держит ПК бодрым (BLOCK):
  * сессия в статусе busy — модель прямо сейчас генерирует ответ;
  * сессия в статусе waiting (выполняется инструмент) И её транскрипт рос за
    последние --stall секунд, то есть работа продолжает выдавать результат;
  * ИДЁТ СБОРКА — процесс из --guard, реально грузящий процессор (см. ниже).

Что НЕ мешает уснуть (по прямой просьбе Вадима — «меня нет за ПК»):
  * idle — агент договорил и ждёт человека;
  * заданный вопрос / запрос разрешения: формально это «выполняется инструмент»,
    но транскрипт при этом не растёт, поэтому «тихий waiting» дольше --stall
    считается ожиданием человека, а не работой.

Про сборки (переделано 31.07 после разбора).
    Первая версия смотрела на факт существования Unity.exe и ограничивала ожидание
    получасом. Вышло худшее из двух миров: подвисший редактор держал бы ПК вечно,
    а живая чужая сборка была усыплена на 592-й секунде и потеряла восемь часов.
    Теперь признак работы — НАГРУЗКА: между опросами считается съеденное процессом
    процессорное время; выше --guard-cpu процентов ядра = сборка идёт. После
    последней нагрузки процесс считается работающим ещё --guard-idle секунд
    (сборки делают паузы на I/O). Идущую сборку не прерывает даже --max-wait —
    этот лимит существует против зависших агентов, а не против работы.
    Поэтому список процессов можно держать широким: простаивающий демон Gradle
    или открытый редактор Unity нагрузки не дают и сну не мешают.

Примеры:
    python sleep_sync.py --status              # разовый отчёт, ничего не делает
    python sleep_sync.py --wait                # ждать тишины и уснуть
    python sleep_sync.py --wait --dry-run      # то же, но вместо сна — запись в лог
    python sleep_sync.py --wait --guard ""     # не ждать фоновых сборок Unity

Отмена уже запущенного ожидания: создать файл ~/.claude/sleep_sync.cancel
    python sleep_sync.py --cancel
"""

import argparse
import ctypes
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

for _stream in (sys.stdout, sys.stderr):     # консоль здесь бывает cp1252
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

HOME = Path(os.path.expanduser("~"))
CLAUDE_DIR = HOME / ".claude"
SESSIONS_DIR = CLAUDE_DIR / "sessions"
PROJECTS_DIR = CLAUDE_DIR / "projects"
LOG_PATH = CLAUDE_DIR / "sleep_sync.log"
LOCK_PATH = CLAUDE_DIR / "sleep_sync.lock"
CANCEL_PATH = CLAUDE_DIR / "sleep_sync.cancel"
REPORT_PY = CLAUDE_DIR / "report.py"

# Свежесть статуса: если сессия давно не обновляла свой файл, а процесс жив —
# считаем статус протухшим и не даём ему блокировать сон вечно.
STATUS_STALE_SEC = 900

# Процессы, за которыми следим как за «идёт сборка». Список широкий намеренно:
# блокирует сон не факт запуска, а нагрузка на процессор, поэтому простаивающий
# демон Gradle или открытый редактор Unity помехой не станут.
DEFAULT_GUARD = ",".join([
    "Unity.exe", "bee_backend.exe", "UnityShaderCompiler.exe", "il2cpp.exe",
    "Unity.ILPP.Runner.exe",                       # Unity, включая Android
    "MSBuild.exe", "cl.exe", "link.exe", "ninja.exe",
    "UnrealBuildTool.exe", "UnrealEditor-Cmd.exe", "ShaderCompileWorker.exe",
    "java.exe", "gradle.exe",
])

# Скрипт живёт под pythonw (без консоли): любой subprocess без этого флага
# открывает мигающее окно cmd/powershell. Опрос идёт каждые 20 с — окна мелькали бы вечно.
CREATE_NO_WINDOW = 0x08000000


# ---------------------------------------------------------------- утилиты ---

def log(msg: str, echo: bool = True) -> None:
    line = "%s  %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass
    if echo:
        try:
            print(line, flush=True)
        except (OSError, UnicodeEncodeError):
            pass


def pid_alive(pid: int) -> bool:
    """Жив ли процесс. Без внешних зависимостей — через OpenProcess."""
    if pid <= 0:
        return False
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return False
    try:
        code = ctypes.c_ulong()
        if k32.GetExitCodeProcess(h, ctypes.byref(code)):
            return code.value == STILL_ACTIVE
        return True
    finally:
        k32.CloseHandle(h)


def transcript_path(session_id: str):
    """Путь к .jsonl сессии — по нему видно, растёт ли работа."""
    if not session_id:
        return None
    for p in PROJECTS_DIR.glob("*/%s.jsonl" % session_id):
        return p
    return None


# ------------------------------------------------------- сборки (гард) -----
#
# «Процесс существует» — плохой признак работы: так подвисший редактор Unity держал
# бы ПК вечно, а таймер-ограничитель, наоборот, усыплял машину посреди живой сборки
# (30.07 ночью так и вышло — сон пришёл на 592-й секунде чужой сборки).
# Поэтому смотрим на ФАКТ ПРОГРЕССА: сколько процессорного времени процесс съел
# между опросами. Идущая сборка грузит ядро, подвисшая — нет.

class _PROCESSENTRY32(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.c_ulong), ("cntUsage", ctypes.c_ulong),
                ("th32ProcessID", ctypes.c_ulong),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", ctypes.c_ulong), ("cntThreads", ctypes.c_ulong),
                ("th32ParentProcessID", ctypes.c_ulong), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.c_ulong), ("szExeFile", ctypes.c_char * 260)]


def list_processes():
    """[(pid, имя)] — через снимок Toolhelp, без запуска tasklist (и без мигающих окон)."""
    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)      # TH32CS_SNAPPROCESS
    if snap == -1:
        return []
    out = []
    try:
        e = _PROCESSENTRY32()
        e.dwSize = ctypes.sizeof(_PROCESSENTRY32)
        ok = k32.Process32First(snap, ctypes.byref(e))
        while ok:
            out.append((int(e.th32ProcessID), e.szExeFile.decode("latin-1")))
            ok = k32.Process32Next(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    return out


def process_cpu_seconds(pid: int):
    """Суммарное процессорное время процесса, с. None — процесс недоступен."""
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, pid)                 # QUERY_LIMITED_INFORMATION
    if not h:
        return None
    try:
        creation = ctypes.c_ulonglong()
        exit_t = ctypes.c_ulonglong()
        kernel = ctypes.c_ulonglong()
        user = ctypes.c_ulonglong()
        if not k32.GetProcessTimes(h, ctypes.byref(creation), ctypes.byref(exit_t),
                                   ctypes.byref(kernel), ctypes.byref(user)):
            return None
        return (kernel.value + user.value) / 1e7            # 100-нс тики → секунды
    finally:
        k32.CloseHandle(h)


class BuildWatch:
    """Держит сон, пока хоть один процесс из списка реально грузит процессор."""

    def __init__(self, names, cpu_min_pct, idle_sec):
        self.names = {n.strip().lower() for n in names if n.strip()}
        self.cpu_min = cpu_min_pct / 100.0
        self.idle_sec = idle_sec
        self._cpu = {}          # pid -> (момент, съеденные секунды CPU)
        self._active = {}       # pid -> когда последний раз был занят

    def poll(self):
        """→ (список активных «PID(имя) NN%», список всех подходящих процессов)"""
        now = time.time()
        found, active = [], []
        for pid, name in list_processes():
            if name.lower() not in self.names:
                continue
            found.append("%s(%d)" % (name, pid))
            cpu = process_cpu_seconds(pid)
            if cpu is None:
                continue
            prev = self._cpu.get(pid)
            self._cpu[pid] = (now, cpu)
            if prev is None:
                continue        # первый замер — только точка отсчёта; нагрузку даст следующий

            dt = now - prev[0]
            if dt <= 0:
                continue
            load = (cpu - prev[1]) / dt         # доля одного ядра
            if load >= self.cpu_min:
                self._active[pid] = now
            since = now - self._active.get(pid, 0)
            if since <= self.idle_sec:
                active.append("%s(%d) %.0f%% CPU" % (name, pid, load * 100))

        alive = {int(p.split("(")[1].rstrip(")")) for p in found}
        for pid in list(self._cpu):
            if pid not in alive:
                self._cpu.pop(pid, None)
                self._active.pop(pid, None)
        return active, found


# --------------------------------------------------------------- сессии ----

def read_sessions():
    """
    Живые сессии Claude Code из ~/.claude/sessions/<pid>.json.

    Файлы пишет сам CLI: status = busy | idle | waiting, плюс statusUpdatedAt.
    Это тот же источник, что у ClaudeDock, но без вызова `claude agents --json`,
    который на загруженном ПК отвечает по 20-30 секунд.
    """
    out = []
    if not SESSIONS_DIR.is_dir():
        return out
    for f in SESSIONS_DIR.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pid = int(d.get("pid") or 0)
        if not pid_alive(pid):
            continue                      # сессия закрыта, файл остался
        sid = d.get("sessionId") or ""
        tp = transcript_path(sid)
        out.append({
            "pid": pid,
            "sid": sid,
            "name": d.get("name") or sid[:8],
            "cwd": d.get("cwd") or "",
            "status": (d.get("status") or "").lower(),
            "status_age": max(0.0, time.time() - (d.get("statusUpdatedAt") or 0) / 1000.0),
            "transcript_age": (time.time() - tp.stat().st_mtime) if tp and tp.exists() else None,
        })
    out.sort(key=lambda s: s["name"])
    return out


def classify(sess, stall_sec):
    """→ (blocks_sleep: bool, reason: str)"""
    st, tage = sess["status"], sess["transcript_age"]

    # Протухший busy/waiting (сессия зависла или потеряла связь) не должен держать сон вечно.
    if st in ("busy", "waiting") and sess["status_age"] > STATUS_STALE_SEC:
        return False, "статус «%s» протух (%.0f мин без обновления)" % (st, sess["status_age"] / 60)

    if st == "busy":
        return True, "думает"

    if st == "waiting":
        if tage is None:
            return True, "выполняет инструмент"
        if tage <= stall_sec:
            return True, "выполняет инструмент (запись %.0fс назад)" % tage
        return False, "тихо %.0f мин — ждёт человека (вопрос/разрешение)" % (tage / 60)

    if st == "idle":
        return False, "договорил, ждёт человека"

    return False, "статус «%s»" % (st or "?")


def poll(stall_sec, watch):
    sessions = read_sessions()
    blockers = []
    for s in sessions:
        blocks, reason = classify(s, stall_sec)
        s["blocks"], s["reason"] = blocks, reason
        if blocks:
            blockers.append("%s: %s" % (s["name"], reason))
    builds, found = watch.poll() if watch else ([], [])
    for b in builds:
        blockers.append("сборка %s" % b)
    return sessions, builds, found, blockers


# ------------------------------------------------------------------ сон ----

def notify(text):
    if not REPORT_PY.exists():
        return
    try:
        subprocess.run([sys.executable, str(REPORT_PY), text],
                       capture_output=True, timeout=60, creationflags=CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        pass


def suspend():
    """S3. Гибернация на этой машине выключена, поэтому bHibernate=False = реальный сон."""
    ctypes.windll.powrprof.SetSuspendState(False, True, False)


# ------------------------------------------------------------------ CLI ----

def print_status(sessions, builds, found, blockers):
    print("Сессии Claude Code (живые): %d" % len(sessions))
    for s in sessions:
        print("  %-6s %-34s pid=%-6d %-8s %s"
              % ("БЛОК" if s["blocks"] else "тихо", s["name"][:34], s["pid"],
                 s["status"], s["reason"]))
    if found:
        print("Сборочные процессы: %s" % ", ".join(found))
        print("  из них активны: %s" % (", ".join(builds) if builds else "нет (простаивают)"))
    print()
    print("ИТОГ: %s" % ("можно спать" if not blockers
                        else "спать нельзя — " + "; ".join(blockers)))


def main(argv=None):
    ap = argparse.ArgumentParser(description="Спящий режим, синхронизированный с агентами Claude Code")
    ap.add_argument("--wait", action="store_true", help="ждать тишины и усыпить ПК")
    ap.add_argument("--status", action="store_true", help="разовый отчёт и выход")
    ap.add_argument("--cancel", action="store_true", help="отменить запущенное ожидание")
    ap.add_argument("--interval", type=int, default=20, help="период опроса, с (20)")
    ap.add_argument("--calm", type=int, default=3, help="подряд тихих опросов до сна (3)")
    ap.add_argument("--stall", type=int, default=180,
                    help="тихий waiting дольше N с = ждёт человека, а не работает (180)")
    ap.add_argument("--grace", type=int, default=60, help="пауза перед сном после решения, с (60)")
    ap.add_argument("--max-wait", type=int, default=4 * 3600,
                    help="через сколько секунд уснуть, если ждём только агентов; 0 = вечно (14400). "
                         "Идущую сборку этот лимит не прерывает")
    ap.add_argument("--guard", default=DEFAULT_GUARD,
                    help="сборочные процессы, через запятую; пустая строка — не ждать сборок")
    ap.add_argument("--guard-cpu", type=float, default=12.0,
                    help="сколько %% ядра процесс должен грузить, чтобы считаться работающим (12)")
    ap.add_argument("--guard-idle", type=int, default=600,
                    help="сколько секунд после последней нагрузки процесс ещё считается живой сборкой (600)")
    ap.add_argument("--dry-run", action="store_true", help="не усыплять, только записать в лог")
    args = ap.parse_args(argv)

    if args.cancel:
        CANCEL_PATH.write_text(datetime.now().isoformat(), encoding="utf-8")
        log("отмена запрошена (--cancel)")
        print("Ожидание сна отменено.")
        return 0

    guard_names = [g for g in (args.guard or "").split(",") if g.strip()]
    watch = BuildWatch(guard_names, args.guard_cpu, args.guard_idle) if guard_names else None

    if args.status or not args.wait:
        if watch:
            watch.poll()                     # первый замер CPU — второй даст нагрузку
            time.sleep(2)
        sessions, builds, found, blockers = poll(args.stall, watch)
        print_status(sessions, builds, found, blockers)
        return 0 if not blockers else 1

    # --- одна копия на машину ---
    if LOCK_PATH.exists():
        try:
            old = int(LOCK_PATH.read_text(encoding="utf-8").strip())
            if pid_alive(old) and old != os.getpid():
                log("уже запущен экземпляр pid=%d — выхожу" % old)
                return 2
        except (OSError, ValueError):
            pass
    LOCK_PATH.write_text(str(os.getpid()), encoding="utf-8")
    if CANCEL_PATH.exists():
        CANCEL_PATH.unlink()

    started = time.time()
    calm = 0
    polls = 0
    log("старт ожидания: interval=%ds calm=%d stall=%ds guard=%s (>=%.0f%% CPU, отпускаем через %dс) "
        "max_wait=%ss dry_run=%s"
        % (args.interval, args.calm, args.stall, guard_names or "нет",
           args.guard_cpu, args.guard_idle, args.max_wait, args.dry_run))

    try:
        while True:
            if CANCEL_PATH.exists():
                log("отменено файлом sleep_sync.cancel — сна не будет")
                return 0

            polls += 1
            sessions, builds, found, blockers = poll(args.stall, watch)
            waited = time.time() - started

            if blockers:
                # лог не должен молчать часами: пишем при срыве тишины и раз в ~5 минут
                if calm or polls % max(1, int(300 / max(1, args.interval))) == 0:
                    log("ждём: %s" % "; ".join(blockers[:4]))
                calm = 0
            else:
                calm += 1
                log("тихо %d/%d (живых сессий %d, ждём %.0f мин)"
                    % (calm, args.calm, len(sessions), waited / 60))

            # Лимит ожидания — против зависших агентов, а НЕ против идущей сборки:
            # 30.07 сон пришёл на 592-й секунде чужой сборки и стоил восьми часов работы.
            forced = 0 < args.max_wait <= waited and not builds
            if calm >= args.calm or forced:
                why = ("лимит ожидания %.1f ч исчерпан" % (args.max_wait / 3600.0)
                       if forced else "все агенты закончили, сборок не идёт")
                log("решение: спать (%s). Живых сессий %d." % (why, len(sessions)))
                notify("😴 ПК уходит в спящий режим: %s.\nЖивых сессий Claude: %d. Ждали %.0f мин."
                       % (why, len(sessions), waited / 60))
                if args.grace:
                    log("пауза %d с перед сном (отмена: sleep_sync.py --cancel)" % args.grace)
                    for _ in range(args.grace):
                        time.sleep(1)
                        if CANCEL_PATH.exists():
                            log("отменено во время паузы — сна не будет")
                            return 0
                if args.dry_run:
                    log("DRY-RUN: сон пропущен")
                    return 0
                log("SetSuspendState")
                suspend()
                time.sleep(20)          # проснулись
                log("ПК проснулся")
                return 0

            time.sleep(args.interval)
    except KeyboardInterrupt:
        log("прервано с клавиатуры")
        return 130
    finally:
        try:
            if LOCK_PATH.exists() and LOCK_PATH.read_text(encoding="utf-8").strip() == str(os.getpid()):
                LOCK_PATH.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    sys.exit(main())
