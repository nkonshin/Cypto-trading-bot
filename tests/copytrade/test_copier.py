import asyncio
import time

from copytrade.copier import OFFER_TTL, Copier
from copytrade.exchange import MarketInfo, PaperAdapter
from copytrade.model import Event, Pos
from copytrade.sizing import fit_margin, floor_step

BTC, ETH = "BTC/USDT:USDT", "ETH/USDT:USDT"


def run(coro):
    return asyncio.run(coro)


def make(equity=200.0, price=59000.0):
    ad = PaperAdapter(markets={BTC: MarketInfo(BTC, 0.0001, 0.0001, 125), ETH: MarketInfo(ETH, 0.01, 0.01, 100)},
                      price_fn=lambda s: price if s == BTC else 3000.0, equity=equity)
    ad.name = "live"          # как боевой адаптер: без пометки [тест]
    return ad, Copier(ad)


def P(side="short", size=0.2, tp=None, sl=None, known=False, margin=245.5788, sym="BTCUSDT", avg=59571.7):
    return Pos(sym, side, size, avg, margin, 59086.1, tp, sl, known)


def texts(msgs):
    return [m.text for m in msgs]


def test_floor_step_and_fit_margin():
    assert floor_step(0.04999, 0.0001) == 0.0499
    assert floor_step((0.3 - 0.2) * 0.5, 0.0001) == 0.05
    assert floor_step(0.00009, 0.0001) == 0.0
    assert fit_margin(0.04, 59000, 50, 200, 0.0001) == (0.04, False)
    amt, cut = fit_margin(1.0, 59000, 50, 200, 0.0001)
    assert cut and amt == floor_step(200 * 0.95 * 50 / 59000, 0.0001)


def test_btc_opens_automatically_with_given_k_and_his_leverage():
    ad, c = make()
    k = 200 / 900                                   # пропорция балансов
    msgs = run(c.on_event(Event("open", "BTCUSDT", None, P(size=0.4, margin=474.1)), k))
    assert ad.positions[BTC] == ("short", 0.0888)  # 0.4 × 200/900 = 0.0889 → вниз до шага
    assert ("prepare", BTC, 50) in ad.orders
    assert "открыл SHORT 0.0888" in msgs[0].text and not msgs[0].buttons


def test_other_coin_is_offered_not_opened():
    ad, c = make()
    eth = P(side="long", size=2.0, sym="ETHUSDT", avg=3000.0, margin=120.0)
    msgs = run(c.on_event(Event("open", "ETHUSDT", None, eth), 0.25))
    assert ETH not in ad.positions
    (m,) = msgs
    assert "Это не BTC" in m.text and [b[0] for b in m.buttons] == ["Открыть", "Пропустить"]
    oid = m.buttons[0][1].split(":")[2]
    out = run(c.answer_offer(oid, True, {"ETHUSDT": eth}, 0.25))
    assert ad.positions[ETH] == ("long", 0.5) and "с опозданием" in out[0].text
    # после подтверждения монета ведётся автоматически: Раст закрыл — закрываем
    run(c.on_event(Event("close", "ETHUSDT", eth, None), 0.25))
    assert ad.positions[ETH] == (None, 0.0)


def test_offer_skip_expired_and_stale():
    ad, c = make()
    eth = P(side="long", size=2.0, sym="ETHUSDT", avg=3000.0, margin=120.0)
    (m,) = run(c.on_event(Event("open", "ETHUSDT", None, eth), 0.25))
    oid = m.buttons[1][1].split(":")[2]
    assert "пропускаю" in run(c.answer_offer(oid, False, {"ETHUSDT": eth}, 0.25))[0].text
    (m,) = run(c.on_event(Event("open", "ETHUSDT", None, eth), 0.25))
    oid = m.buttons[0][1].split(":")[2]
    c.offers[oid].created -= OFFER_TTL + 1
    assert "неактуально" in run(c.answer_offer(oid, True, {"ETHUSDT": eth}, 0.25))[0].text
    (m,) = run(c.on_event(Event("open", "ETHUSDT", None, eth), 0.25))
    oid = m.buttons[0][1].split(":")[2]
    assert "уже закрыл" in run(c.answer_offer(oid, True, {}, 0.25))[0].text   # Раст успел закрыть
    assert ETH not in ad.positions


def test_increase_adds_his_delta_times_current_k():
    ad, c = make()
    run(c.on_event(Event("open", "BTCUSDT", None, P(size=0.2)), 0.2))
    run(c.on_event(Event("increase", "BTCUSDT", P(size=0.2), P(size=0.3)), 0.5))
    assert ad.positions[BTC] == ("short", 0.09)       # 0.04 + 0.1×0.5


def test_decrease_is_proportional_to_our_position():
    ad, c = make()
    run(c.on_event(Event("open", "BTCUSDT", None, P(size=0.2)), 0.2))
    run(c.on_event(Event("decrease", "BTCUSDT", P(size=0.2), P(size=0.05)), None))   # k не нужен
    assert ad.positions[BTC] == ("short", 0.01)


def test_close_and_flip():
    ad, c = make()
    run(c.on_event(Event("open", "BTCUSDT", None, P()), 0.2))
    run(c.on_event(Event("flip", "BTCUSDT", P(), P(side="long", size=0.1)), 0.2))
    assert ad.positions[BTC] == ("long", 0.02)
    run(c.on_event(Event("close", "BTCUSDT", P(side="long", size=0.1), None), 0.2))
    assert ad.positions[BTC] == (None, 0.0) and "BTCUSDT" not in c.syms


def test_unknown_balance_means_no_open():
    ad, c = make()
    msgs = run(c.on_event(Event("open", "BTCUSDT", None, P()), None))
    assert BTC not in ad.positions and "не знаю его баланс" in msgs[0].text


def test_below_min_lot_and_margin_cap():
    ad, c = make()
    assert "меньше минимального лота" in run(c.on_event(Event("open", "BTCUSDT", None, P(size=0.0004)), 0.2))[0].text
    ad, c = make(equity=20.0)
    msgs = run(c.on_event(Event("open", "BTCUSDT", None, P(size=1.0, margin=1191.4)), 0.2))
    _, amt = ad.positions[BTC]
    assert 0 < amt * 59000 / 50 <= 20 * 0.95 and "ужал под маржу" in msgs[0].text


def test_inconsistent_margin_means_default_leverage():
    ad, c = make()
    run(c.on_event(Event("open", "BTCUSDT", None, P(size=1.0)), 0.2))  # 242x — не стандарт → 10x
    assert ("prepare", BTC, 10) in ad.orders


def test_uncopied_positions_only_reported():
    ad, c = make()
    msgs = run(c.on_event(Event("increase", "BTCUSDT", P(), P(size=0.3)), 0.2))
    assert "не копирую" in msgs[0].text and BTC not in ad.positions
    msgs = run(c.on_event(Event("close", "BTCUSDT", P(size=0.3), None), 0.2))
    assert "не копировали" in msgs[0].text


def test_manual_positions_on_other_coins_are_never_touched():
    ad, c = make()
    ad.positions[ETH] = ("long", 3.0)                 # Никита открыл руками
    run(c.on_event(Event("open", "BTCUSDT", None, P()), 0.2))
    run(c.close_all())
    run(c.on_event(Event("close", "ETHUSDT", P(sym="ETHUSDT", side="long", size=1, avg=3000, margin=60), None), 0.2))
    assert ad.positions[ETH] == ("long", 3.0)


def test_drift_detaches_and_his_close_leaves_yours():
    ad, c = make()
    run(c.on_event(Event("open", "BTCUSDT", None, P()), 0.2))
    ad.positions[BTC] = ("short", 0.1)                 # ты долил руками
    msgs = run(c.on_event(Event("increase", "BTCUSDT", P(), P(size=0.3)), 0.2))
    assert "разошлась" in msgs[0].text and ad.positions[BTC] == ("short", 0.1)
    msgs = run(c.on_event(Event("close", "BTCUSDT", P(size=0.3), None), 0.2))
    assert ad.positions[BTC] == ("short", 0.1) and "не трогаю" in msgs[0].text
    assert "BTCUSDT" not in c.syms


def test_tpsl_update_replaces_stops_on_full_amount():
    ad, c = make()
    run(c.on_event(Event("open", "BTCUSDT", None, P(tp=54321.0, sl=59750.0, known=True)), 0.2))
    assert ad.stops[BTC] == {"tp": 54321.0, "sl": 59750.0, "amount": 0.04}
    run(c.on_event(Event("tpsl", "BTCUSDT", P(), P(tp=54321.0, sl=59400.0, known=True)), None))
    assert ad.stops[BTC]["sl"] == 59400.0
    run(c.on_event(Event("increase", "BTCUSDT", P(tp=54321.0, sl=59400.0, known=True),
                         P(size=0.3, tp=54321.0, sl=59400.0, known=True)), 0.2))
    assert ad.stops[BTC]["amount"] == 0.06


def test_resync_offers_open_and_close():
    ad, c = make()
    # ничего ни у кого
    (m,) = run(c.resync({}, 0.2, "Сверка:"))
    assert "Позиций нет ни у Раста, ни у тебя" in m.text
    # у Раста есть, у нас нет → предложение открыть
    msgs = run(c.resync({"BTCUSDT": P(size=0.4)}, 0.2, "Сверка:"))
    assert len(msgs) == 2 and "Открыть пропорционально" in msgs[1].text and msgs[1].buttons
    # у нас копия, Раст закрыл во время паузы → предложение закрыть / оставить себе
    run(c.on_event(Event("open", "BTCUSDT", None, P()), 0.2))
    msgs = run(c.resync({}, 0.2, "Сверка:"))
    offer = msgs[-1]
    assert "Закрыть?" in offer.text and [b[0] for b in offer.buttons] == ["Закрыть копию", "Оставить себе"]
    oid = offer.buttons[1][1].split(":")[2]
    run(c.answer_offer(oid, False, {}, 0.2))
    assert ad.positions[BTC] == ("short", 0.04) and "BTCUSDT" not in c.syms     # оставили себе


def test_resync_releases_positions_changed_by_hand_during_pause():
    ad, c = make()
    run(c.on_event(Event("open", "BTCUSDT", None, P()), 0.2))
    ad.positions[BTC] = ("short", 0.02)                # в паузе прикрыл половину руками
    msgs = run(c.resync({"BTCUSDT": P()}, 0.2, "Сверка:"))
    assert "считаю твоей" in msgs[0].text and "BTCUSDT" not in c.syms


def test_paper_messages_are_tagged():
    ad = PaperAdapter(markets={BTC: MarketInfo(BTC, 0.0001, 0.0001)}, price_fn=lambda s: 59000.0)
    msgs = run(Copier(ad).on_event(Event("open", "BTCUSDT", None, P()), 0.2))
    assert msgs[0].text.startswith("[тест] ")
