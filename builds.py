# -*- coding: utf-8 -*-
"""Which projects are building right now — the dock's «Сборки» strip (0 tokens).

Processes are recognised by the build fleet's own scanner (`machine_status.py` in the
folder set as `fleet_dir`): Unity in batchmode by its -projectPath, Gradle, emulators,
build loops and keepers queued on the RAM gate. This module only groups them per
project and gives each a short label. No `fleet_dir` configured → nothing to show.
"""
import importlib.util
import os

import config

_ms = None
_ms_error = None

# Strip labels, most important first: one chip per project shows its top-ranked activity.
RANK = {"APK": 0, "PC": 0, "сборка": 0, "gradle": 1, "тесты": 2, "batch": 3, "импорт": 3,
        "эмул.": 4, "петля": 5, "ждёт": 6}


def _machine_status():
    """Load machine_status.py once (it needs psutil and exits without it)."""
    global _ms, _ms_error
    if _ms is not None or _ms_error is not None:
        return _ms
    fleet = config.get("fleet_dir")
    if not fleet:
        _ms_error = "fleet_dir not set"
        return None
    try:
        spec = importlib.util.spec_from_file_location("fleet_machine_status",
                                                      os.path.join(fleet, "machine_status.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _ms = mod
    except BaseException as ex:       # SystemExit when psutil is missing
        _ms_error = repr(ex)
    return _ms


def _label(p):
    act = p.get("activity") or ""
    kind = p.get("kind")
    if kind == "unity":
        up = act.upper()
        if "СБОРКА ANDROID" in up:
            return "APK"
        if "СБОРКА STANDALONE" in up or "СБОРКА WIN" in up:
            return "PC"
        if "СБОРКА" in up:
            return "сборка"
        if "автотест" in act:
            return "тесты"
        if "импорт" in act:
            return "импорт"
        return "batch"
    return {"java/gradle": "gradle", "emulator": "эмул.", "loop": "петля",
            "queue": "ждёт"}.get(kind, kind or "?")


def scan():
    """{"items": [{project, label, parts, activity, pid, age_min, ram_gb, path}], "ram_free_gb", "lock"}"""
    ms = _machine_status()
    if ms is None:
        return {"items": [], "ram_free_gb": None, "lock": None, "error": _ms_error}
    registry = ms.load_registry()
    paths = {name: path for name, path in registry}
    procs, _ = ms.scan_processes(registry)

    by_proj = {}
    for p in procs:
        if p["kind"] == "helper":
            continue
        if p["kind"] == "unity" and "редактор с окном" in (p.get("activity") or ""):
            continue                       # an open editor is not a build
        proj = p.get("project") or "?"
        lab = _label(p)
        cur = by_proj.get(proj)
        if cur is None:
            by_proj[proj] = {"project": proj, "label": lab, "parts": [lab],
                             "activity": p.get("activity"), "pid": p["pid"],
                             "age_min": p.get("age_min", 0), "ram_gb": p.get("ram_gb", 0.0),
                             "path": paths.get(proj)}
            continue
        cur["parts"].append(lab)
        cur["ram_gb"] = round(cur["ram_gb"] + p.get("ram_gb", 0.0), 2)
        if RANK.get(lab, 9) < RANK.get(cur["label"], 9):
            cur.update(label=lab, activity=p.get("activity"), pid=p["pid"], age_min=p.get("age_min", 0))

    lock = None
    try:
        lock = next((l for l in ms.scan_locks()
                     if l["file"].lower().startswith("build") and l.get("owner_alive")), None)
    except Exception:
        pass
    try:
        import psutil
        ram = round(psutil.virtual_memory().available / 1024 ** 3, 1)
    except Exception:
        ram = None

    items = sorted(by_proj.values(), key=lambda e: (RANK.get(e["label"], 9), -e["age_min"]))
    return {"items": items, "ram_free_gb": ram, "lock": lock}


if __name__ == "__main__":
    import json
    print(json.dumps(scan(), ensure_ascii=False, indent=1))
