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


def test_mult_buttons_and_command():
    p = panel()
    u, ctx = cb_update(ADMIN, "copy:mult:+")
    run(p.on_callback(u, ctx))
    assert shared.load_settings()["mult"] == 1.1
    u, ctx = msg_update(ADMIN, ["0,5"])
    run(p.cmd_mult(u, ctx))
    assert shared.load_settings()["mult"] == 0.5
    u, ctx = msg_update(ADMIN, ["99"])
    run(p.cmd_mult(u, ctx))
    assert shared.load_settings()["mult"] == 0.5


def test_pause_buttons_and_resume():
    from telegram_ui.copy_panel import until_2300
    p = panel()
    u, ctx = cb_update(ADMIN, "copy:pause:4h")
    run(p.on_callback(u, ctx))
    assert 4 * 3600 - 5 < shared.load_settings()["paused_until"] - time.time() <= 4 * 3600
    u, ctx = cb_update(ADMIN, "copy:resume")
    run(p.on_callback(u, ctx))
    assert shared.load_settings()["paused_until"] == 0
    u, ctx = cb_update(ADMIN, "copy:pause:inf")
    run(p.on_callback(u, ctx))
    assert shared.load_settings()["paused_until"] == -1
    t = until_2300(time.mktime((2026, 11, 25, 23, 30, 0, 0, 0, -1)))
    assert time.localtime(t).tm_mday == 26 and time.localtime(t).tm_hour == 23


def test_offer_button_becomes_command():
    p = panel()
    q_upd, ctx = cb_update(ADMIN, "copy:offer:abc123:yes")
    q_upd.callback_query.edit_message_reply_markup = AsyncMock()
    run(p.on_callback(q_upd, ctx))
    (cmd,) = shared.load_settings()["commands"]
    assert (cmd["name"], cmd["offer"], cmd["yes"]) == ("offer", "abc123", True)
    q_upd.callback_query.edit_message_reply_markup.assert_called_once()


def test_close_all_sets_one_shot_command():
    p = panel()
    u, ctx = cb_update(ADMIN, "copy:closeall")
    run(p.on_callback(u, ctx))
    assert shared.load_settings()["commands"] == []             # сначала подтверждение
    u, ctx = cb_update(ADMIN, "copy:closeall:yes")
    run(p.on_callback(u, ctx))
    assert shared.load_settings()["commands"][-1]["name"] == "close_all"


def test_status_text_renders_live_status():
    from telegram_ui.copy_panel import status_text
    assert "не отвечает" in status_text()
    shared.write_status({"mode": "notify", "online": True, "seen_ok": True, "reason": "",
                         "his_positions": [{"symbol": "BTCUSDT", "side": "short", "size": 0.4, "avg": 59345.0,
                                            "leverage": 50, "tp": 54321.0, "sl": 59600.0}],
                         "his_balance": 815.6503, "our_positions": [], "our_equity": 200.0, "k": 0.2452,
                         "events": [{"t": time.time(), "text": "Вижу панель Раста"}]})
    t = status_text()
    assert "Раст: SHORT BTCUSDT 0.4 BTC (~$23 738) по 59 345 (50x) · TP 54 321 / SL 59 600" in t
    assert "Баланс: его 815.65 · твой 200 USDT · k сейчас 0.245" in t
    assert "PnL" not in t                                          # цены ещё нет — без PnL
    shared.save_settings(paused_until=-1)
    assert "на паузе без срока" in status_text()



def test_auto_toggle():
    p = panel()
    assert shared.load_settings()["auto"] is False
    u, ctx = cb_update(ADMIN, "copy:auto")
    run(p.on_callback(u, ctx))
    assert shared.load_settings()["auto"] is True
    u, ctx = cb_update(FATHER, "copy:auto")
    run(p.on_callback(u, ctx))
    assert shared.load_settings()["auto"] is True



def his_status(ours=()):
    shared.write_status({"mode": "notify", "online": True, "seen_ok": True, "reason": "",
                         "his_positions": [{"symbol": "BTCUSDT", "side": "short", "size": 0.4, "avg": 59345.0,
                                            "leverage": 50, "tp": None, "sl": None, "price": 59000.0,
                                            "pnl": 138.0, "pnl_pct": 29.07},
                                           {"symbol": "BTCUSDT", "side": "long", "size": 0.2, "avg": 58669.3,
                                            "leverage": 50, "tp": None, "sl": None, "price": 59000.0,
                                            "pnl": 66.14, "pnl_pct": 28.2}],
                         "his_balance": 900.0, "our_positions": list(ours), "our_equity": 200.0, "k": 0.22,
                         "events": []})


def test_status_shows_his_pnl():
    from telegram_ui.copy_panel import status_text
    his_status()
    t = status_text()
    assert "SHORT BTCUSDT 0.4 BTC (~$23 738) по 59 345 (50x) · PnL +$138 (+29.1%)" in t


def test_enter_buttons_for_uncopied_positions():
    from telegram_ui.copy_panel import keyboard
    his_status(ours=[{"symbol": "BTCUSDT", "side": "short", "amount": 0.08, "detached": False}])
    btns = [b for row in keyboard().inline_keyboard for b in row if b.callback_data.startswith("copy:enter:")]
    assert [(b.text, b.callback_data) for b in btns] == [("➕ Войти LONG BTCUSDT", "copy:enter:BTCUSDT:long")]
    shared.save_settings(mode="off")                                # без копировщика входить некуда
    assert not [b for row in keyboard().inline_keyboard for b in row if b.callback_data.startswith("copy:enter:")]


def test_enter_button_becomes_command():
    p = panel()
    his_status()
    u, ctx = cb_update(ADMIN, "copy:enter:BTCUSDT:long")
    run(p.on_callback(u, ctx))
    cmd = shared.load_settings()["commands"][-1]
    assert (cmd["name"], cmd["symbol"], cmd["side"]) == ("enter", "BTCUSDT", "long")
    u, ctx = cb_update(FATHER, "copy:enter:BTCUSDT:short")
    run(p.on_callback(u, ctx))
    assert len(shared.load_settings()["commands"]) == 1
