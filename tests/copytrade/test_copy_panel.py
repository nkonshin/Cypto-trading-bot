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
    import datetime as dt
    t = until_2300(dt.datetime(2026, 11, 25, 23, 30, tzinfo=shared.PERM).timestamp())
    perm = dt.datetime.fromtimestamp(t, shared.PERM)
    assert (perm.day, perm.hour, perm.minute) == (26, 23, 0)               # 23:00 по Перми, не по серверу (UTC)
    t = until_2300(dt.datetime(2026, 11, 25, 17, 24, tzinfo=dt.timezone.utc).timestamp())   # 22:24 в Перми
    assert dt.datetime.fromtimestamp(t, shared.PERM).day == 25


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



def test_offer_keep_choice_becomes_command():
    p = panel()
    q_upd, ctx = cb_update(ADMIN, "copy:offer:abc123:keep")
    q_upd.callback_query.edit_message_reply_markup = AsyncMock()
    run(p.on_callback(q_upd, ctx))
    (cmd,) = shared.load_settings()["commands"]
    assert (cmd["offer"], cmd["yes"], cmd["choice"]) == ("abc123", True, "keep")



def perm_ts(h, m, sec=0):
    import datetime as dt
    return dt.datetime(2026, 11, 25, h, m, sec, tzinfo=shared.PERM).timestamp()


def test_times_are_perm_not_server_utc():
    from telegram_ui.copy_panel import pause_text, status_text
    assert shared.perm_time(perm_ts(21, 5)) == "21:05"
    shared.write_status({"mode": "notify", "online": True, "seen_ok": True, "his_positions": [],
                         "our_positions": [], "events": [{"t": perm_ts(21, 5), "text": "Сверка позиций:\n…"}]})
    assert "21:05 Сверка позиций:" in status_text()
    assert pause_text(time.time() + 3600) == "на паузе до " + shared.perm_time(time.time() + 3600)


def full_status(now, **extra):
    shared.write_status({
        "mode": "live", "online": True, "seen_ok": True, "reason": "",
        "last_frame": now - 3, "last_ocr": now - 8, "last_ok": now - 8, "last_balance": now - 240,
        "his_positions": [{"symbol": "BTCUSDT", "side": "long", "size": 0.4, "avg": 57650.0, "leverage": 50,
                           "tp": None, "sl": None, "tpsl_known": True, "price": 58776.8,
                           "pnl": 450.72, "pnl_pct": 97.7}],
        "his_balance": 1214.37, "our_equity": 154.87, "k": 0.1275,
        "our_positions": [], "own_positions": [{"symbol": "BTCUSDT", "side": "long", "amount": 0.0604,
                                                "entry": 57931.8, "pnl": 51.0, "pnl_pct": 29.1}],
        "events": [{"t": now - 600, "text": "Сверка позиций:\nдальше"}], **extra})


def test_status_brief_shows_health_and_position():
    from telegram_ui.copy_panel import status_brief
    now = perm_ts(22, 24, 30)
    full_status(now)
    shared.save_settings(mode="live")
    t = status_brief(now)
    assert "📊 <b>Копирование Раста</b> · 22:24 Пермь" in t
    assert "Режим: <b>ЖИВЫЕ ДЕНЬГИ BingX</b> · авто выкл — всё через кнопки" in t
    assert "Стрим: 🟢 в эфире · панель видна ✅" in t
    assert "Последний кадр: 22:24:27 (3 с назад)" in t
    assert "Последнее чтение панели: 22:24:22 (8 с назад)" in t
    assert "Баланс Раста: 1 214.37 USDT, прочитан 22:20 (4 мин назад)" in t
    assert "Твой баланс: 154.87 USDT · k 0.128" in t and "условный" not in t
    assert "🟢 <b>LONG BTCUSDT</b> · 50x" in t
    assert "Раст: 0.4 BTC (~$23 060), вход 57 650" in t
    assert "Цена 58 776.8 · PnL <b>+$451 (+97.7%)</b>" in t
    assert "TP/SL не стоят" in t
    assert "У тебя своя, не копия: 0.0604 BTC (~$3 550), вход 57 931.8, PnL +$51 (+29.1%)" in t
    assert "22:14 Сверка позиций:" in t and "дальше" not in t


def test_status_brief_blind_copy_and_orphans():
    from telegram_ui.copy_panel import status_brief
    now = perm_ts(22, 24, 30)
    full_status(now, mode="notify", seen_ok=False, reason="панель <скрыта>", last_ok=now - 300,
                our_positions=[{"symbol": "BTCUSDT", "side": "long", "amount": 0.05, "detached": False,
                                "entry": None, "pnl": None},
                               {"symbol": "ETHUSDT", "side": "short", "amount": 1.0, "detached": True,
                                "entry": 3000.0, "pnl": -12.5}],
                own_positions=[])
    shared.save_settings(mode="notify")
    t = status_brief(now)
    assert "⚠️ панель не видна: панель &lt;скрыта&gt;" in t                     # HTML экранирован
    assert "Панель видел последний раз: 22:19:30 (5 мин назад)" in t
    assert "(условный: режим без сделок)" in t
    assert "Твоя копия: 0.05 BTC (~$2 939)" in t
    assert ("Копия без позиции у Раста: 🔴 SHORT ETHUSDT 1 ETH (~$3 000), вход 3 000, PnL -$12 "
            "(отцеплена, не трогаю)") in t


def test_status_brief_when_copier_is_down():
    from telegram_ui.copy_panel import status_brief
    assert "Копировщик не отвечает" in status_brief()
    shared.write_status({"mode": "notify"})
    assert "последний статус" in status_brief(time.time() + 120)


def test_status_command_and_buttons():
    from telegram_ui.copy_panel import keyboard
    full_status(time.time())
    p = panel()
    u, ctx = msg_update(ADMIN)
    run(p.cmd_status(u, ctx))
    args, kw = u.message.reply_text.call_args
    assert "Копирование Раста" in args[0] and kw["parse_mode"] == "HTML"
    assert [b.callback_data for row in kw["reply_markup"].inline_keyboard for b in row] == \
        ["copy:status", "copy:shot", "copy:refresh"]
    u, ctx = msg_update(FATHER)
    run(p.cmd_status(u, ctx))
    u.message.reply_text.assert_not_called()
    data = [b.callback_data for row in keyboard().inline_keyboard for b in row]
    assert "copy:status" in data and "copy:shot" in data
    u, ctx = cb_update(ADMIN, "copy:status")
    run(p.on_callback(u, ctx))
    args, kw = u.callback_query.edit_message_text.call_args
    assert "Последний кадр" in args[0] and kw["parse_mode"] == "HTML"


def test_shot_asks_copier_and_sends_photo(data_dir):
    from telegram_ui.copy_panel import CopyPanel

    async def copier():                       # копировщик отвечает на команду кадром
        for _ in range(100):
            cmds = shared.load_settings()["commands"]
            if cmds:
                (data_dir / "snapshot.jpg").write_bytes(b"jpg")
                shared.write_snapshot_info({"cmd": cmds[-1]["id"], "ok": True, "t": time.time() - 2,
                                            "online": True, "seen": "панель видна"})
                return
            await asyncio.sleep(0.05)

    msg = SimpleNamespace(reply_photo=AsyncMock(), reply_text=AsyncMock())

    async def both():
        await asyncio.gather(CopyPanel._send_shot(msg, timeout=3), copier())
    run(both())
    kw = msg.reply_photo.call_args.kwargs
    assert kw["caption"].startswith("📸 Кадр со стрима ") and "Пермь (2 с назад) · в эфире · панель видна" in kw["caption"]
    assert shared.load_settings()["commands"][-1]["name"] == "snapshot"


def test_shot_reports_silent_copier():
    from telegram_ui.copy_panel import CopyPanel
    msg = SimpleNamespace(reply_photo=AsyncMock(), reply_text=AsyncMock())
    run(CopyPanel._send_shot(msg, timeout=0.5))
    msg.reply_photo.assert_not_called()
    assert "не ответил" in msg.reply_text.call_args[0][0]


def test_guard_buttons_and_command():
    from telegram_ui.copy_panel import keyboard, status_brief, status_text
    his_status(ours=[{"symbol": "BTCUSDT", "side": "short", "amount": 0.08, "detached": False,
                      "no_add": True, "sl_tighten": False, "tp": 55000.0, "sl": 60000.0}])
    btns = [(b.text, b.callback_data) for row in keyboard().inline_keyboard for b in row
            if b.callback_data.startswith("copy:guard:")]
    assert btns == [("▶️ SHORT BTC: снова доливать", "copy:guard:BTCUSDT:short:no_add:0"),
                    ("🛡 SHORT BTC: SL только подтягивать", "copy:guard:BTCUSDT:short:sl_tighten:1")]
    assert "Копия: SHORT BTCUSDT 0.08 BTC" in status_text() and "не доливаю" in status_text()
    assert "Стопы бота: TP 55 000 · SL 60 000 · 🛡 не доливаю" in status_brief()
    p = panel()
    u, ctx = cb_update(ADMIN, "copy:guard:BTCUSDT:short:sl_tighten:1")
    run(p.on_callback(u, ctx))
    cmd = shared.load_settings()["commands"][-1]
    assert (cmd["name"], cmd["symbol"], cmd["side"], cmd["sl_tighten"]) == ("guard", "BTCUSDT", "short", True)
    assert "no_add" not in cmd
    u, ctx = cb_update(FATHER, "copy:guard:BTCUSDT:short:no_add:1")
    run(p.on_callback(u, ctx))
    assert len(shared.load_settings()["commands"]) == 1
