import asyncio

import pytest

from copytrade.copier import Copier
from copytrade.exchange import MarketInfo, PaperAdapter
from copytrade.model import Event, Pos
from copytrade.sizing import fit_margin, floor_step

BTC = "BTC/USDT:USDT"


def run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def make(equity=200.0, price=59000.0):
    ad = PaperAdapter(markets={BTC: MarketInfo(BTC, 0.0001, 0.0001, 125)}, price_fn=lambda s: price, equity=equity)
    ad.name = "live"          # как боевой адаптер: без пометки [тест]
    return ad, Copier(ad)


def P(side="short", size=0.2, tp=None, sl=None, known=False, margin=245.5788):
    return Pos("BTCUSDT", side, size, 59571.7, margin, 59086.1, tp, sl, known)


def test_floor_step_and_fit_margin():
    assert floor_step(0.04999, 0.0001) == 0.0499
    assert floor_step(0.2 * 0.2, 0.0001) == 0.04
    assert floor_step(0.00009, 0.0001) == 0.0
    assert fit_margin(0.04, 59000, 50, 200, 0.0001) == (0.04, False)
    amt, cut = fit_margin(1.0, 59000, 50, 200, 0.0001)
    assert cut and amt == floor_step(200 * 0.95 * 50 / 59000, 0.0001)


def test_open_scales_by_k_and_uses_his_leverage():
    ad, c = make()
    msgs = run(c.on_event(Event("open", "BTCUSDT", None, P()), 0.2))
    assert ad.positions[BTC] == ("short", 0.04)
    assert ("prepare", BTC, 50) in ad.orders
    assert "открыл SHORT 0.04" in msgs[0]


def test_open_with_known_tpsl_places_stops():
    ad, c = make()
    run(c.on_event(Event("open", "BTCUSDT", None, P(tp=54321.0, sl=59750.0, known=True)), 0.2))
    assert ad.stops[BTC] == {"tp": 54321.0, "sl": 59750.0, "amount": 0.04}


def test_increase_adds_his_delta_times_current_k():
    ad, c = make()
    run(c.on_event(Event("open", "BTCUSDT", None, P(size=0.2)), 0.2))
    run(c.on_event(Event("increase", "BTCUSDT", P(size=0.2), P(size=0.3)), 0.5))
    assert ad.positions[BTC] == ("short", 0.09)       # 0.04 + 0.1×0.5


def test_decrease_is_proportional_to_our_position():
    ad, c = make()
    run(c.on_event(Event("open", "BTCUSDT", None, P(size=0.2)), 0.2))
    run(c.on_event(Event("decrease", "BTCUSDT", P(size=0.2), P(size=0.05)), 0.9))   # k не влияет
    assert ad.positions[BTC] == ("short", 0.01)


def test_close_and_flip():
    ad, c = make()
    run(c.on_event(Event("open", "BTCUSDT", None, P()), 0.2))
    run(c.on_event(Event("flip", "BTCUSDT", P(), P(side="long", size=0.1)), 0.2))
    assert ad.positions[BTC] == ("long", 0.02)
    run(c.on_event(Event("close", "BTCUSDT", P(side="long", size=0.1), None), 0.2))
    assert ad.positions[BTC] == (None, 0.0)
    assert "BTCUSDT" not in c.syms


def test_below_min_lot_is_skipped():
    ad, c = make()
    msgs = run(c.on_event(Event("open", "BTCUSDT", None, P(size=0.0004)), 0.2))
    assert BTC not in ad.positions and "меньше минимального лота" in msgs[0]


def test_margin_cap_shrinks_size():
    ad, c = make(equity=20.0)
    big = P(size=1.0, margin=1191.4)                                   # 50x
    msgs = run(c.on_event(Event("open", "BTCUSDT", None, big), 0.2))   # 0.2 BTC × 59000 / 50 = 236 > 19
    side, amt = ad.positions[BTC]
    assert 0 < amt * 59000 / 50 <= 20 * 0.95 and "ужал под маржу" in msgs[0]


def test_inconsistent_margin_means_default_leverage():
    ad, c = make()
    run(c.on_event(Event("open", "BTCUSDT", None, P(size=1.0)), 0.2))  # 242x — не стандарт → по умолчанию 10x
    assert ("prepare", BTC, 10) in ad.orders


def test_baseline_positions_are_ignored_until_closed():
    ad, c = make()
    msgs = run(c.on_baseline((P(),), 0.2, join_existing=False))
    assert BTC not in ad.positions and "не вхожу" in msgs[0]
    assert run(c.on_event(Event("increase", "BTCUSDT", P(), P(size=0.3)), 0.2)) == []
    run(c.on_event(Event("close", "BTCUSDT", P(size=0.3), None), 0.2))
    run(c.on_event(Event("open", "BTCUSDT", None, P(side="long", size=0.1)), 0.2))
    assert ad.positions[BTC] == ("long", 0.02)


def test_baseline_join_existing_opens_late():
    ad, c = make()
    msgs = run(c.on_baseline((P(),), 0.2, join_existing=True))
    assert ad.positions[BTC] == ("short", 0.04) and "с опозданием" in msgs[0]


def test_drift_detaches_symbol_until_his_close():
    ad, c = make()
    run(c.on_event(Event("open", "BTCUSDT", None, P()), 0.2))
    ad.positions[BTC] = (None, 0.0)                    # у нас сработал стоп, у него нет
    msgs = run(c.on_event(Event("increase", "BTCUSDT", P(), P(size=0.3)), 0.2))
    assert "разошлась" in msgs[0] and ad.positions[BTC] == (None, 0.0)
    assert run(c.on_event(Event("decrease", "BTCUSDT", P(size=0.3), P(size=0.1)), 0.2)) == []
    run(c.on_event(Event("close", "BTCUSDT", P(size=0.1), None), 0.2))
    assert "BTCUSDT" not in c.syms                     # после его закрытия снова чисто


def test_tpsl_update_replaces_stops_on_full_amount():
    ad, c = make()
    run(c.on_event(Event("open", "BTCUSDT", None, P()), 0.2))
    run(c.on_event(Event("tpsl", "BTCUSDT", P(), P(tp=54321.0, sl=59750.0, known=True)), 0.2))
    assert ad.stops[BTC]["sl"] == 59750.0
    run(c.on_event(Event("increase", "BTCUSDT", P(tp=54321.0, sl=59750.0, known=True),
                         P(size=0.3, tp=54321.0, sl=59750.0, known=True)), 0.2))
    assert ad.stops[BTC]["amount"] == 0.06              # стопы пересозданы на новый объём


def test_close_all():
    ad, c = make()
    run(c.on_event(Event("open", "BTCUSDT", None, P()), 0.2))
    msgs = run(c.close_all())
    assert ad.positions[BTC] == (None, 0.0) and "Закрываю копию" in msgs[0]


def test_unknown_symbol():
    ad, c = make()
    p = Pos("FOOUSDT", "long", 10, 1.0, 1.0)
    msgs = run(c.on_event(Event("open", "FOOUSDT", None, p), 0.2))
    assert "монеты нет на BingX" in msgs[0]


def test_paper_messages_are_tagged():
    ad = PaperAdapter(markets={BTC: MarketInfo(BTC, 0.0001, 0.0001)}, price_fn=lambda s: 59000.0)
    c = Copier(ad)
    msgs = run(c.on_event(Event("open", "BTCUSDT", None, P()), 0.2))
    assert msgs[0].startswith("[тест] ")
