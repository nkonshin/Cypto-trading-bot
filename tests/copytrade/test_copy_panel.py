import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from copytrade import shared

ADMIN, FATHER = 208210577, 111


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(shared, "DATA_DIR", str(tmp_path))
    yield tmp_path


def panel():
    from telegram_ui.copy_panel import CopyPanel
    return CopyPanel(lambda uid: uid == ADMIN)


def msg_update(uid, args=None):
    msg = SimpleNamespace(reply_text=AsyncMock())
    return SimpleNamespace(effective_user=SimpleNamespace(id=uid), message=msg), SimpleNamespace(args=args or [])


def cb_update(uid, data):
    q = SimpleNamespace(data=data, from_user=SimpleNamespace(id=uid), answer=AsyncMock(), edit_message_text=AsyncMock())
    return SimpleNamespace(callback_query=q), SimpleNamespace()


def run(c):
    return asyncio.run(c)


def test_father_gets_nothing():
    u, ctx = msg_update(FATHER)
    run(panel().cmd_copy(u, ctx))
    u.message.reply_text.assert_not_called()
    u, ctx = cb_update(FATHER, "copy:mode:live")
    run(panel().on_callback(u, ctx))
    u.callback_query.answer.assert_called_once_with("⛔ Только для админа")
    assert shared.load_settings()["mode"] == "notify"


def test_live_requires_confirmation():
    p = panel()
    u, ctx = cb_update(ADMIN, "copy:mode:live")
    run(p.on_callback(u, ctx))
    assert shared.load_settings()["mode"] == "notify"
    assert "ЖИВЫЕ" in u.callback_query.edit_message_text.call_args[0][0]
    u, ctx = cb_update(ADMIN, "copy:live:yes")
    run(p.on_callback(u, ctx))
    assert shared.load_settings()["mode"] == "live"


def test_k_buttons_and_command():
    p = panel()
    u, ctx = cb_update(ADMIN, "copy:k:+")
    run(p.on_callback(u, ctx))
    assert shared.load_settings()["k"] == 0.25
    u, ctx = msg_update(ADMIN, ["0,3"])
    run(p.cmd_k(u, ctx))
    assert shared.load_settings()["k"] == 0.3
    u, ctx = msg_update(ADMIN, ["99"])
    run(p.cmd_k(u, ctx))
    assert shared.load_settings()["k"] == 0.3


def test_close_all_sets_one_shot_command():
    p = panel()
    u, ctx = cb_update(ADMIN, "copy:closeall")
    run(p.on_callback(u, ctx))
    assert shared.load_settings()["command"] is None            # сначала подтверждение
    u, ctx = cb_update(ADMIN, "copy:closeall:yes")
    run(p.on_callback(u, ctx))
    assert shared.load_settings()["command"]["name"] == "close_all"


def test_status_text_renders_live_status():
    from telegram_ui.copy_panel import status_text
    assert "не отвечает" in status_text()
    shared.write_status({"mode": "notify", "online": True, "seen_ok": True, "reason": "",
                         "his_positions": [{"symbol": "BTCUSDT", "side": "short", "size": 0.4, "avg": 59345.0,
                                            "leverage": 50, "tp": 54321.0, "sl": 59600.0}],
                         "his_balance": 815.6503, "our_positions": [], "ignored": ["BTCUSDT"], "our_equity": 200.0,
                         "events": [{"t": time.time(), "text": "Вижу панель Раста"}]})
    t = status_text()
    assert "Раст: SHORT BTCUSDT 0.4 по 59 345 (50x) · TP 54 321 / SL 59 600" in t
    assert "BTCUSDT не копирую" in t and "Пропорция балансов: 0.245" in t
