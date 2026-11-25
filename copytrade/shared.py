"""Общие файлы с крипто-ботом (том copier-data): settings.json пишет бот, status.json — копировщик."""
from __future__ import annotations

import datetime as dt
import json
import os
import time

DATA_DIR = os.environ.get("COPIER_DATA", "/app/copier_data")
PERM = dt.timezone(dt.timedelta(hours=5))      # всё время в сообщениях — пермское (серверы живут в UTC)
MODES = ("off", "notify", "demo", "live")
DEFAULTS = {
    "mode": "notify",          # off | notify | demo | live
    "auto": False,             # False — каждое действие с кнопками «Делаем/Не делаем»; True — BTC сам
    "mult": 1.0,               # объём = его объём × (наш баланс / его баланс) × mult
    "auto_symbols": ["BTCUSDT"],   # копируются сами; остальные монеты — через кнопку
    "paused_until": 0,         # 0 — не на паузе, -1 — бессрочно, иначе unix-время окончания
    "default_leverage": 10,    # если его плечо не прочиталось
    "max_leverage": 50,        # потолок плеча у нас
    "paper_equity": 200,       # баланс для режима notify
    "commands": [],            # разовые команды из бота: [{"id", "t", "name", ...}]
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
        s["mult"] = min(3.0, max(0.05, float(s["mult"])))
    except (TypeError, ValueError):
        s["mult"] = DEFAULTS["mult"]
    if not isinstance(s.get("auto_symbols"), list):
        s["auto_symbols"] = list(DEFAULTS["auto_symbols"])
    return s


def save_settings(**changes) -> dict:
    s = {**load_settings(), **changes, "updated": time.time()}
    _write("settings.json", s)
    return s


def add_command(name: str, **args) -> dict:
    """Разовая команда копировщику (хранятся последние 20)."""
    import uuid
    cmds = (load_settings().get("commands") or [])[-19:]
    cmds.append({"id": uuid.uuid4().hex, "t": time.time(), "name": name, **args})
    return save_settings(commands=cmds)


def write_status(status: dict) -> None:
    _write("status.json", {**status, "updated": time.time()})


def read_status() -> dict:
    return _read("status.json")


def perm_time(ts: float, fmt: str = "%H:%M") -> str:
    return dt.datetime.fromtimestamp(ts, PERM).strftime(fmt)


def ago(ts: float, now: float = None) -> str:
    d = max(0, int((now or time.time()) - ts))
    if d < 60:
        return f"{d} с назад"
    if d < 3600:
        return f"{d // 60} мин назад"
    return f"{d // 3600} ч {d % 3600 // 60} мин назад"


def snapshot_path() -> str:
    """Кадр со стрима по кнопке «📸»: копировщик пишет, бот отправляет."""
    return _path("snapshot.jpg")


def write_snapshot_info(info: dict) -> None:
    _write("snapshot.json", info)


def read_snapshot_info() -> dict:
    return _read("snapshot.json")
