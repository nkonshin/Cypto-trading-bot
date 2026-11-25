"""Главный цикл без стрима: пауза, сверка, кнопки, пропорция от балансов."""
import asyncio
import time

import pytest

from copytrade import shared
from copytrade.exchange import MarketInfo, PaperAdapter
from copytrade.model import Event, Pos

BTC = "BTC/USDT:USDT"


class FakeTG:
    def __init__(self):
        self.sent = []

    async def send(self, msg):
        self.sent.append(msg)

    def texts(self):
        return [getattr(m, "text", m) for m in self.sent]


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setattr(shared, "DATA_DIR", str(tmp_path))
    from copytrade import main as m

    def fake_paper(equity=200.0, hedged=False):
        return PaperAdapter(markets={BTC: MarketInfo(BTC, 0.0001, 0.0001)}, price_fn=lambda s: 59000.0, equity=equity)

    monkeypatch.setattr(m, "PaperAdapter", fake_paper)
    a = m.App(source=object(), reader=object(), notifier=FakeTG())
    asyncio.run(a.set_mode("notify"))
    a.his_balance, a.last_balance = 900.0, time.time()
    return a


def P(size=0.4, side="short"):
    return Pos("BTCUSDT", side, size, 59345.0, 474.76, 59000.0)


def confirm(app, *positions):
    app.tracker.confirmed = {(p.symbol, p.side): p for p in positions}


def test_k_from_current_balances_and_multiplier(app):
    assert asyncio.run(app.k_now()) == pytest.approx(200 / 900)
    shared.save_settings(mult=0.5)
    asyncio.run(app.apply_settings())
    assert asyncio.run(app.k_now()) == pytest.approx(100 / 900)
    app.last_balance -= 16 * 60                       # его баланс устарел — пропорцию не считаем
    assert asyncio.run(app.k_now()) is None


def test_open_uses_balance_ratio(app):
    shared.save_settings(auto=True)
    asyncio.run(app.apply_settings())
    confirm(app, P())
    asyncio.run(app.on_event(Event("open", "BTCUSDT", None, P())))
    assert app.adapter.positions[BTC] == ("short", 0.0888)


def test_pause_blocks_trading_and_resume_offers_entry(app):
    shared.save_settings(paused_until=time.time() + 3600)
    asyncio.run(app.apply_settings())
    assert any("Пауза до" in t for t in app.tg.texts())
    confirm(app, P())
    asyncio.run(app.on_event(Event("open", "BTCUSDT", None, P())))
    assert BTC not in app.adapter.positions
    assert "пауза — не трогаю" in app.tg.texts()[-1]
    # продолжить → сверка с предложением войти
    shared.save_settings(paused_until=0)
    asyncio.run(app.apply_settings())
    last = app.tg.sent[-1]
    assert "Открыть по рынку" in last.text and last.buttons
    # нажатие кнопки приходит командой из бота
    oid = last.buttons[0][1].split(":")[2]
    shared.add_command("offer", offer=oid, yes=True)
    asyncio.run(app.apply_settings())
    assert app.adapter.positions[BTC] == ("short", 0.0888)


def test_timed_pause_expires_by_itself(app):
    shared.save_settings(paused_until=time.time() - 1)
    app.was_paused = True
    asyncio.run(app.apply_settings())
    assert shared.load_settings()["paused_until"] == 0
    assert any("Пауза закончилась" in t for t in app.tg.texts())


def test_old_commands_are_not_replayed_after_restart(app):
    cmds = [{"id": "x1", "t": time.time() - 3600, "name": "close_all"}]
    shared.save_settings(commands=cmds, auto=True)
    asyncio.run(app.apply_settings())
    asyncio.run(app.on_event(Event("open", "BTCUSDT", None, P())))
    asyncio.run(app.apply_settings())
    assert app.adapter.positions[BTC][1] > 0         # часовой давности close_all не выполнился


def test_copies_survive_restart_in_live_modes(app, tmp_path):
    from copytrade import main as m
    shared.save_settings(auto=True)
    asyncio.run(app.apply_settings())
    asyncio.run(app.on_event(Event("open", "BTCUSDT", None, P())))
    app.mode = "demo"                              # делаем вид, что это биржевой режим
    app.save_copies()
    fresh = m.App(source=object(), reader=object(), notifier=FakeTG())
    asyncio.run(fresh.set_mode("notify"))
    fresh.mode = "demo"
    fresh.load_copies()
    assert fresh.copier.syms[("BTCUSDT", "short")].expected == 0.0888
    # после перезапуска сверка не предлагает «Открыть» поверх уже открытой копии
    fresh.adapter.positions["BTC/USDT:USDT"] = ("short", 0.0888)
    msgs = asyncio.run(fresh.copier.resync({("BTCUSDT", "short"): P()}, 0.2, "Сверка:"))
    assert len(msgs) == 1 and "копия на месте" in msgs[0].text


def test_commands_from_before_start_are_not_executed(tmp_path, monkeypatch):
    monkeypatch.setattr(shared, "DATA_DIR", str(tmp_path))
    shared.add_command("close_all")                        # нажали перед перезапуском
    from copytrade import main as m
    a = m.App(source=object(), reader=object(), notifier=FakeTG())
    assert shared.load_settings()["commands"][0]["id"] in a.done_commands



def test_default_is_confirm_each_action(app):
    assert shared.load_settings()["auto"] is False
    confirm(app, P())
    asyncio.run(app.on_event(Event("open", "BTCUSDT", None, P())))
    assert BTC not in app.adapter.positions
    last = app.tg.sent[-1]
    assert "Копируем?" in last.text and last.buttons
    shared.add_command("offer", offer=last.buttons[0][1].split(":")[2], yes=True)
    asyncio.run(app.apply_settings())
    assert app.adapter.positions[BTC] == ("short", 0.0888)
    assert app.tg.sent[-1].text.startswith("[тест] ✅ Готово")



def test_enter_command_from_menu_sends_entry_offer(app):
    confirm(app, P())
    shared.add_command("enter", symbol="BTCUSDT", side="short")
    asyncio.run(app.apply_settings())
    last = app.tg.sent[-1]
    assert [b[0] for b in last.buttons] == ["✅ Войти", "✖️ Не входить"] and "PnL Раста" in last.text
    assert BTC not in app.adapter.positions


def test_status_has_his_pnl_at_live_price(app):
    from types import SimpleNamespace
    app.source = SimpleNamespace(online=True)
    confirm(app, P())                                  # SHORT 0.4 по 59 345, маржа 474.76; цена 59 000
    asyncio.run(app.refresh_prices())
    app.write_status()
    (p,) = shared.read_status()["his_positions"]
    assert p["price"] == 59000.0 and round(p["pnl"], 2) == 138.0 and round(p["pnl_pct"], 1) == 29.1


def test_update_on_declined_position_comes_with_estimate(app):
    confirm(app, P(size=0.2))
    asyncio.run(app.on_event(Event("decrease", "BTCUSDT", P(), P(size=0.2))))
    last = app.tg.sent[-1]
    assert last.buttons and "у тебя будет SHORT" in last.text      # k посчитан и для сокращения



def test_three_buttons_go_one_per_row():
    from copytrade.main import keyboard_rows
    two = (("✅ Делаем", "a"), ("✖️ Не делаем", "b"))
    assert keyboard_rows(two) == [[{"text": "✅ Делаем", "callback_data": "a"},
                                   {"text": "✖️ Не делаем", "callback_data": "b"}]]
    three = two + (("Ничего", "c"),)
    assert [len(r) for r in keyboard_rows(three)] == [1, 1, 1]


def test_offer_command_passes_choice(app):
    app.adapter.positions[BTC] = ("long", 0.05)        # одна позиция на монету (one-way)
    confirm(app, P())
    shared.add_command("enter", symbol="BTCUSDT", side="short")
    asyncio.run(app.apply_settings())
    last = app.tg.sent[-1]
    assert last.buttons[0][0] == "✅ Закрыть LONG и войти в SHORT"
    shared.add_command("offer", offer=last.buttons[0][1].split(":")[2], yes=True, choice="yes")
    asyncio.run(app.apply_settings())
    assert app.adapter.positions[BTC] == ("short", 0.0888)



def test_turning_auto_on_sends_resync(app):
    confirm(app, P())
    app.adapter.positions[BTC] = ("short", 0.05)       # твоя позиция в ту же сторону
    shared.save_settings(auto=True)
    asyncio.run(app.apply_settings())
    texts = app.tg.texts()
    assert any("Автомат включён" in t for t in texts)
    assert app.tg.sent[-1].buttons[0][0] == "✅ Вести как копию"



def test_snapshot_command_writes_last_frame(app, tmp_path):
    import numpy as np
    from types import SimpleNamespace
    app.source = SimpleNamespace(online=True, last_error="")
    shared.add_command("snapshot")
    asyncio.run(app.apply_settings())
    info = shared.read_snapshot_info()
    assert info["ok"] is False and "кадров со стрима ещё не было" in info["reason"]
    app.latest, app.latest_t = np.zeros((90, 160, 3), np.uint8), 123.0
    cid = shared.add_command("snapshot")["commands"][-1]["id"]
    asyncio.run(app.apply_settings())
    info = shared.read_snapshot_info()
    assert (info["cmd"], info["ok"], info["t"], info["online"]) == (cid, True, 123.0, True)
    assert (tmp_path / "snapshot.jpg").read_bytes()[:2] == b"\xff\xd8"          # JPEG


def test_status_has_times_and_own_positions(app):
    from types import SimpleNamespace
    app.source = SimpleNamespace(online=True)
    confirm(app, P(side="long"))
    app.adapter.positions[BTC] = ("long", 0.05)                       # своя позиция, бот её не ведёт

    async def positions(sym):
        return [{"side": "long", "amount": 0.05, "entry": 58000.0, "pnl": 1.0, "leverage": 10.0}]
    app.adapter.open_positions = positions
    app.latest_t, app.last_ocr = 100.0, 99.0
    asyncio.run(app.refresh_prices())
    asyncio.run(app.refresh_exchange())
    app.write_status()
    st = shared.read_status()
    assert (st["last_frame"], st["last_ocr"]) == (100.0, 99.0) and st["our_positions"] == []
    (o,) = st["own_positions"]
    assert (o["side"], o["amount"], o["entry"]) == ("long", 0.05, 58000.0)
    assert round(o["pnl"], 2) == 50.0 and round(o["pnl_pct"], 1) == 17.2   # по цене 59 000, маржа 290
    assert st["his_positions"][0]["tpsl_known"] is False


def test_pause_message_in_perm_time(app):
    until = time.time() + 3600
    shared.save_settings(paused_until=until)
    asyncio.run(app.apply_settings())
    assert any(f"Пауза до {shared.perm_time(until)}" in t for t in app.tg.texts())


def test_guard_command_sets_flags_and_status_shows_them(app):
    from types import SimpleNamespace
    app.source = SimpleNamespace(online=True)
    shared.save_settings(auto=True)
    asyncio.run(app.apply_settings())
    asyncio.run(app.on_event(Event("open", "BTCUSDT", None, P())))
    shared.add_command("guard", symbol="BTCUSDT", side="short", no_add=True)
    asyncio.run(app.apply_settings())
    assert "доливки Раста НЕ повторяю" in app.tg.texts()[-1]
    shared.add_command("guard", symbol="BTCUSDT", side="short", sl_tighten=True)
    asyncio.run(app.apply_settings())
    st = app.copier.syms[("BTCUSDT", "short")]
    assert st.no_add and st.sl_tighten                       # вторая команда не сбросила первую
    app.write_status()
    (o,) = shared.read_status()["our_positions"]
    assert o["no_add"] and o["sl_tighten"]



def test_set_sl_command(app):
    shared.save_settings(auto=True)
    asyncio.run(app.apply_settings())
    asyncio.run(app.on_event(Event("open", "BTCUSDT", None, P())))    # SHORT, цена 59 000
    shared.add_command("set_sl", symbol="BTCUSDT", side="short", sl=59800.0)
    asyncio.run(app.apply_settings())
    assert "SL SHORT BTCUSDT → 59 800" in app.tg.texts()[-1]
    assert app.adapter.stops[BTC]["sl"] == 59800.0
