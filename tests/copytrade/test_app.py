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

    def fake_paper(equity=200.0):
        return PaperAdapter(markets={BTC: MarketInfo(BTC, 0.0001, 0.0001)}, price_fn=lambda s: 59000.0, equity=equity)

    monkeypatch.setattr(m, "PaperAdapter", fake_paper)
    a = m.App(source=object(), reader=object(), notifier=FakeTG())
    asyncio.run(a.set_mode("notify"))
    a.his_balance, a.last_balance = 900.0, time.time()
    return a


def P(size=0.4, side="short"):
    return Pos("BTCUSDT", side, size, 59345.0, 474.76, 59000.0)


def confirm(app, *positions):
    app.tracker.confirmed = {p.symbol: p for p in positions}


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
    assert fresh.copier.syms["BTCUSDT"].expected == 0.0888
    # после перезапуска сверка не предлагает «Открыть» поверх уже открытой копии
    fresh.adapter.positions["BTC/USDT:USDT"] = ("short", 0.0888)
    msgs = asyncio.run(fresh.copier.resync({"BTCUSDT": P()}, 0.2, "Сверка:"))
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
