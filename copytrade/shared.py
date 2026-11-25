"""Общие файлы с крипто-ботом (том copier-data): settings.json пишет бот, status.json — копировщик."""
from __future__ import annotations

import json
import os
import time

DATA_DIR = os.environ.get("COPIER_DATA", "/app/copier_data")
MODES = ("off", "notify", "demo", "live")
DEFAULTS = {
    "mode": "notify",          # off | notify | demo | live
    "k": 0.2,                  # наш объём = его объём × k
    "join_existing": False,    # входить ли в позиции, открытые до старта
    "default_leverage": 10,    # если его плечо не прочиталось
    "max_leverage": 50,        # потолок плеча у нас
    "command": None,           # {"name": "close_all", "id": "..."} — разовая команда из бота
}


def _path(name: str) -> str:
    return os.path.join(DATA_DIR, name)


def _write(name: str, data: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = _path(name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, _path(name))


def _read(name: str) -> dict:
    try:
        with open(_path(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def load_settings() -> dict:
    s = {**DEFAULTS, **_read("settings.json")}
    if s["mode"] not in MODES:
        s["mode"] = "notify"
    try:
        s["k"] = min(5.0, max(0.001, float(s["k"])))
    except (TypeError, ValueError):
        s["k"] = DEFAULTS["k"]
    return s


def save_settings(**changes) -> dict:
    s = {**load_settings(), **changes, "updated": time.time()}
    _write("settings.json", s)
    return s


def write_status(status: dict) -> None:
    _write("status.json", {**status, "updated": time.time()})


def read_status() -> dict:
    return _read("status.json")
