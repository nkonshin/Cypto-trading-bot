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


def rk(*ps):
    """Позиции Раста как в трекере: (монета, сторона) -> Pos."""
    return {(p.symbol, p.side): p for p in ps}


def has(c, sym):
    return any(k[0] == sym for k in c.syms)


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
    assert "Это не BTC" in m.text and [b[0] for b in m.buttons] == ["✅ Делаем", "✖️ Не делаем"]
    oid = m.buttons[0][1].split(":")[2]
    out = run(c.answer_offer(oid, True, rk(eth), 0.25))
    assert ad.positions[ETH] == ("long", 0.5) and out[0].text.startswith("✅ Готово")
    # после подтверждения монета ведётся автоматически: Раст закрыл — закрываем
    run(c.on_event(Event("close", "ETHUSDT", eth, None), 0.25))
    assert ad.positions[ETH] == (None, 0.0)


def test_offer_skip_expired_and_stale():
    ad, c = make()
    eth = P(side="long", size=2.0, sym="ETHUSDT", avg=3000.0, margin=120.0)
    (m,) = run(c.on_event(Event("open", "ETHUSDT", None, eth), 0.25))
    oid = m.buttons[1][1].split(":")[2]
    assert "ничего не делаю" in run(c.answer_offer(oid, False, rk(eth), 0.25))[0].text
    (m,) = run(c.on_event(Event("open", "ETHUSDT", None, eth), 0.25))
    oid = m.buttons[0][1].split(":")[2]
    c.offers[oid].created -= OFFER_TTL + 1
    assert "неактуально" in run(c.answer_offer(oid, True, rk(eth), 0.25))[0].text
    (m,) = run(c.on_event(Event("open", "ETHUSDT", None, eth), 0.25))
    oid = m.buttons[0][1].split(":")[2]
    assert "делать нечего" in run(c.answer_offer(oid, True, {}, 0.25))[0].text   # Раст успел закрыть
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
    run(c.on_event(Event("close", "BTCUSDT", P(), None), 0.2))                 # переворот = закрыть …
    run(c.on_event(Event("open", "BTCUSDT", None, P(side="long", size=0.1)), 0.2))  # … и открыть
    assert ad.positions[BTC] == ("long", 0.02)
    run(c.on_event(Event("close", "BTCUSDT", P(side="long", size=0.1), None), 0.2))
    assert ad.positions[BTC] == (None, 0.0) and not has(c, "BTCUSDT")


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


def test_uncopied_position_update_offers_entry_and_close_reports_his_result():
    ad, c = make()                                    # цена на бирже 59 000, у него вход 59 571.7
    msgs = run(c.on_event(Event("increase", "BTCUSDT", P(), P(size=0.3, margin=368.37)), 0.2))
    (m,) = msgs
    assert BTC not in ad.positions and [b[0] for b in m.buttons] == ["✅ Войти", "✖️ Не входить"]
    assert "PnL Раста сейчас: +$172 (+48.0%) при цене 59 000" in m.text
    msgs = run(c.on_event(Event("close", "BTCUSDT", P(size=0.3, margin=368.37), None), 0.2))
    assert "не копировали" in msgs[0].text and "+$172" in msgs[0].text and not msgs[0].buttons


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
    assert not has(c, "BTCUSDT")


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
    msgs = run(c.resync(rk(P(size=0.4)), 0.2, "Сверка:"))
    assert len(msgs) == 2 and "Открыть по рынку" in msgs[1].text and msgs[1].buttons
    assert "PnL Раста сейчас: +$" in msgs[1].text
    # у нас копия, Раст закрыл во время паузы → предложение закрыть / оставить себе
    run(c.on_event(Event("open", "BTCUSDT", None, P()), 0.2))
    msgs = run(c.resync({}, 0.2, "Сверка:"))
    offer = msgs[-1]
    assert "Закрыть?" in offer.text and [b[0] for b in offer.buttons] == ["✅ Закрыть", "✖️ Оставить себе"]
    oid = offer.buttons[1][1].split(":")[2]
    run(c.answer_offer(oid, False, {}, 0.2))
    assert ad.positions[BTC] == ("short", 0.04) and not has(c, "BTCUSDT")     # оставили себе


def test_resync_releases_positions_changed_by_hand_during_pause():
    ad, c = make()
    run(c.on_event(Event("open", "BTCUSDT", None, P()), 0.2))
    ad.positions[BTC] = ("short", 0.02)                # в паузе прикрыл половину руками
    msgs = run(c.resync(rk(P()), 0.2, "Сверка:"))
    assert "считаю твоей" in msgs[0].text and not has(c, "BTCUSDT")


def test_paper_messages_are_tagged():
    ad = PaperAdapter(markets={BTC: MarketInfo(BTC, 0.0001, 0.0001)}, price_fn=lambda s: 59000.0)
    msgs = run(Copier(ad).on_event(Event("open", "BTCUSDT", None, P()), 0.2))
    assert msgs[0].text.startswith("[тест] ")


def test_never_merges_copy_into_own_manual_position():
    ad, c = make()
    ad.positions[ETH] = ("long", 2.43)                 # как на реальном счёте Никиты
    eth = P(side="long", size=2.0, sym="ETHUSDT", avg=3000.0, margin=120.0)
    (m,) = run(c.on_event(Event("open", "ETHUSDT", None, eth), 0.25))
    assert labels(m) == ["✅ Вести как копию", "✖️ Не трогать"]       # не «Делаем»: поверх своей не открываю
    ad.positions[BTC] = ("short", 0.01)                # и для автоматического BTC тоже
    (m,) = run(c.on_event(Event("open", "BTCUSDT", None, P()), 0.2))
    assert labels(m) == ["✅ Вести как копию", "✖️ Не трогать"] and ad.positions[BTC] == ("short", 0.01)
    # если своя позиция появилась уже после предложения «Войти» — _open всё равно не сольёт
    ad.positions[BTC] = (None, 0.0)
    (m,) = run(c.offer_entry(("BTCUSDT", "short"), P(), 0.2))
    ad.positions[BTC] = ("short", 0.01)
    out = run(c.answer_offer(offer_id(m), True, rk(P()), 0.2))
    assert "своя позиция" in out[0].text and ad.positions[BTC] == ("short", 0.01)



# ---------- режим подтверждений (auto=False) ----------

def offer_id(msg, yes=True):
    return msg.buttons[0 if yes else 1][1].split(":")[2]


def test_confirm_mode_btc_open_waits_for_button():
    ad, c = make()
    (m,) = run(c.on_event(Event("open", "BTCUSDT", None, P(size=0.4, margin=474.1)), 0.2, auto=False))
    assert BTC not in ad.positions and "Копируем?" in m.text and "SHORT 0.08 BTC (~$4 727), маржа ~$95 при 50x" in m.text
    out = run(c.answer_offer(offer_id(m), True, rk(P(size=0.4, margin=474.1)), 0.15))
    assert ad.positions[BTC] == ("short", 0.06)                # k на момент нажатия
    assert out[0].text.startswith("✅ Готово") and "открыл SHORT 0.06" in out[0].text


def test_confirm_uses_his_current_position_not_the_old_one():
    ad, c = make()
    (m,) = run(c.on_event(Event("open", "BTCUSDT", None, P(size=0.2)), 0.2, auto=False))
    # пока Никита не нажал, Раст долил до 0.4 — «Делаем» откроет уже от 0.4
    run(c.answer_offer(offer_id(m), True, rk(P(size=0.4)), 0.2))
    assert ad.positions[BTC] == ("short", 0.08)


def test_confirm_resize_and_close_and_decline():
    ad, c = make()
    run(c.on_event(Event("open", "BTCUSDT", None, P(size=0.2)), 0.2))          # открыта копия 0.04
    (m,) = run(c.on_event(Event("increase", "BTCUSDT", P(size=0.2), P(size=0.4)), 0.2, auto=False))
    assert "станет ~0.08" in m.text and ad.positions[BTC] == ("short", 0.04)
    assert "ничего не делаю" in run(c.answer_offer(offer_id(m, False), False, {}, 0.2))[0].text
    (m,) = run(c.on_event(Event("decrease", "BTCUSDT", P(size=0.4), P(size=0.1)), None, auto=False))
    run(c.answer_offer(offer_id(m), True, rk(P(size=0.1)), None))
    assert ad.positions[BTC] == ("short", 0.02)                # 0.04 × 0.1/0.2 (его 0.4 мы не повторяли)
    (m,) = run(c.on_event(Event("close", "BTCUSDT", P(size=0.1), None), None, auto=False))
    assert [b[0] for b in m.buttons] == ["✅ Делаем", "✖️ Оставить себе"]
    run(c.answer_offer(offer_id(m), True, {}, None))
    assert ad.positions[BTC] == (None, 0.0)


def test_confirm_close_keep_for_yourself():
    ad, c = make()
    run(c.on_event(Event("open", "BTCUSDT", None, P()), 0.2))
    (m,) = run(c.on_event(Event("close", "BTCUSDT", P(), None), None, auto=False))
    out = run(c.answer_offer(offer_id(m, False), False, {}, None))
    assert "оставляю тебе" in out[0].text and ad.positions[BTC] == ("short", 0.04) and not has(c, "BTCUSDT")


def test_confirm_tpsl_move():
    ad, c = make()
    run(c.on_event(Event("open", "BTCUSDT", None, P(tp=54321.0, sl=59750.0, known=True)), 0.2))
    new = P(tp=54321.0, sl=59400.0, known=True)
    (m,) = run(c.on_event(Event("tpsl", "BTCUSDT", P(), new), None, auto=False))
    assert "Переставить" in m.text and ad.stops[BTC]["sl"] == 59750.0
    run(c.answer_offer(offer_id(m), True, rk(new), None))
    assert ad.stops[BTC]["sl"] == 59400.0


def test_newer_offer_supersedes_older():
    ad, c = make()
    (m1,) = run(c.on_event(Event("open", "BTCUSDT", None, P(size=0.2)), 0.2, auto=False))
    (m2,) = run(c.on_event(Event("open", "BTCUSDT", None, P(size=0.3)), 0.2, auto=False))
    assert "неактуально" in run(c.answer_offer(offer_id(m1), True, {}, 0.2))[0].text
    run(c.answer_offer(offer_id(m2), True, rk(P(size=0.3)), 0.2))
    assert ad.positions[BTC] == ("short", 0.06)



def test_hedge_long_and_short_copies_are_independent():
    """Как у Раста 25.11: SHORT 0.4 → сократил до 0.2 и открыл LONG 0.2 по той же монете."""
    ad = PaperAdapter(markets={BTC: MarketInfo(BTC, 0.0001, 0.0001)}, price_fn=lambda s: 58700.0, hedged=True)
    ad.name = "live"
    c = Copier(ad)
    short4 = P(size=0.4, margin=474.1)
    run(c.on_event(Event("open", "BTCUSDT", None, short4), 0.2))
    run(c.on_event(Event("decrease", "BTCUSDT", short4, P(size=0.2, margin=237.0)), None))
    long2 = P(side="long", size=0.2, avg=58669.3, margin=246.5)
    run(c.on_event(Event("open", "BTCUSDT", None, long2), 0.2))
    assert ad.hpos == {(BTC, "short"): 0.04, (BTC, "long"): 0.04}
    assert set(c.syms) == {("BTCUSDT", "short"), ("BTCUSDT", "long")}
    # закрыл лонг — шорт не трогаем
    run(c.on_event(Event("close", "BTCUSDT", long2, None), None))
    assert ad.hpos == {(BTC, "short"): 0.04}
    # сохранение и восстановление учёта хранит обе стороны раздельно
    c2 = Copier(ad)
    c2.restore(c.dump())
    assert set(c2.syms) == {("BTCUSDT", "short")}



# ---------- повторное предложение войти и PnL Раста ----------

def test_declined_position_is_reoffered_on_every_update():
    ad, c = make()
    (m,) = run(c.on_event(Event("open", "BTCUSDT", None, P()), 0.2, auto=False))
    out = run(c.answer_offer(offer_id(m, False), False, rk(P()), 0.2))          # «Не делаем»
    assert "снова предложу войти" in out[0].text
    p3 = P(size=0.3, margin=368.37)
    p3s = P(size=0.3, margin=368.37, tp=54321.0, sl=60000.0, known=True)
    p1 = P(size=0.1, margin=122.79, tp=54321.0, sl=60000.0, known=True)
    for ev in (Event("increase", "BTCUSDT", P(), p3), Event("tpsl", "BTCUSDT", p3, p3s),
               Event("decrease", "BTCUSDT", p3s, p1)):
        for auto in (False, True):                     # и в автомате вход посреди позиции — только кнопкой
            (m,) = run(c.on_event(ev, 0.2, auto=auto))
            assert [b[0] for b in m.buttons] == ["✅ Войти", "✖️ Не входить"], ev.kind
            assert "У тебя копии нет" in m.text and "PnL Раста сейчас: +$" in m.text
            assert "у тебя будет SHORT" in m.text and BTC not in ad.positions
    # «Войти» — от его позиции на момент нажатия, со стопами
    out = run(c.answer_offer(offer_id(m), True, rk(p1), 0.2))
    assert ad.positions[BTC] == ("short", 0.02) and out[0].text.startswith("✅ Готово")
    assert ad.stops[BTC] == {"tp": 54321.0, "sl": 60000.0, "amount": 0.02}


def test_offer_entry_from_menu():
    ad, c = make()
    (m,) = run(c.offer_entry(("BTCUSDT", "short"), P(), 0.2))
    assert [b[0] for b in m.buttons] == ["✅ Войти", "✖️ Не входить"]
    assert "PnL Раста сейчас: +$114 (+48.0%)" in m.text and BTC not in ad.positions
    run(c.answer_offer(offer_id(m), True, rk(P()), 0.2))
    assert ad.positions[BTC] == ("short", 0.04)
    (m,) = run(c.offer_entry(("BTCUSDT", "short"), P(), 0.2))
    assert "уже" in m.text and not m.buttons
    (m,) = run(c.offer_entry(("BTCUSDT", "long"), None, 0.2))
    assert "нет" in m.text and not m.buttons


def test_his_pnl_long_short_and_loss():
    from copytrade.copier import his_pnl, pnl_str
    assert pnl_str(*his_pnl(P(), 59000.0)) == "+$114 (+48.0%)"
    assert pnl_str(*his_pnl(P(side="long"), 59000.0)) == "-$114 (-48.0%)"
    assert pnl_str(*his_pnl(P(margin=None), 59571.7 - 25)) == "+$5.00"
    assert his_pnl(P(), None) is None


def test_offers_on_copied_position_show_his_pnl():
    ad, c = make()
    run(c.on_event(Event("open", "BTCUSDT", None, P()), 0.2))
    (m,) = run(c.on_event(Event("increase", "BTCUSDT", P(), P(size=0.4, margin=491.16)), 0.2, auto=False))
    assert "PnL Раста сейчас: +$229 (+48.0%)" in m.text
    (m,) = run(c.on_event(Event("close", "BTCUSDT", P(size=0.4, margin=491.16), None), None, auto=False))
    assert "+$229" in m.text



def test_pnl_percent_is_from_initial_margin_like_his_panel():
    """Кадр 25.11 15:26: LONG 0.4 по 57 903.3, 50x, Margin 662.6963 (доливал), mark 58 114.7 → +84.56 (18.25%)."""
    from copytrade.copier import his_pnl
    p = Pos("BTCUSDT", "long", 0.4, 57903.3, 662.6963, 58114.7, lev=50)
    v, pct = his_pnl(p, 58114.7)
    assert round(v, 2) == 84.56 and round(pct, 2) == 18.25



# ---------- у Никиты уже есть своя позиция по монете ----------

def hedge(price=58700.0):
    ad = PaperAdapter(markets={BTC: MarketInfo(BTC, 0.0001, 0.0001)}, price_fn=lambda s: price, hedged=True)
    ad.name = "live"
    return ad, Copier(ad)


LONG4 = P(side="long", size=0.4, avg=57903.3, margin=463.23)


def labels(m):
    return [b[0] for b in m.buttons]


def test_own_opposite_position_offers_close_and_enter():
    for auto in (False, True):                        # и в автомате: при твоей встречной позиции — только кнопкой
        ad, c = hedge()
        ad.hpos[(BTC, "short")] = 0.05                # твой ручной шорт
        (m,) = run(c.on_event(Event("open", "BTCUSDT", None, LONG4), 0.15, auto=auto))
        assert m.text.startswith("⚠️") and "у тебя открыт свой SHORT BTCUSDT 0.05 BTC" in m.text
        assert labels(m) == ["✅ Закрыть SHORT и войти в LONG", "Войти, SHORT оставить", "✖️ Ничего"]
        assert m.buttons[1][1].endswith(":keep") and ad.hpos == {(BTC, "short"): 0.05}
    out = run(c.answer_offer(offer_id(m), True, rk(LONG4), 0.15, choice="yes"))
    assert ad.hpos == {(BTC, "long"): 0.06}
    assert "закрыл твой SHORT 0.05 BTC" in out[0].text and "открыл LONG 0.06" in out[1].text
    assert set(c.syms) == {("BTCUSDT", "long")}       # копией считается только лонг


def test_own_opposite_keep_it_and_enter():
    ad, c = hedge()
    ad.hpos[(BTC, "short")] = 0.05
    (m,) = run(c.on_event(Event("open", "BTCUSDT", None, LONG4), 0.15, auto=False))
    run(c.answer_offer(m.buttons[1][1].split(":")[2], True, rk(LONG4), 0.15, choice="keep"))
    assert ad.hpos == {(BTC, "short"): 0.05, (BTC, "long"): 0.06}
    # Раст открыл и шорт: твой шорт — в ту же сторону (предложу вести как копию), а копия лонга — не «твоя»
    (m,) = run(c.on_event(Event("open", "BTCUSDT", None, P(size=0.2)), 0.15, auto=False))
    assert labels(m) == ["✅ Вести как копию", "✖️ Не трогать"]


def test_own_same_side_position_is_offered_to_be_followed_as_copy():
    for auto in (False, True):
        ad, c = hedge()
        ad.hpos[(BTC, "long")] = 0.03
        (m,) = run(c.on_event(Event("open", "BTCUSDT", None, LONG4), 0.15, auto=auto))
        assert m.text.startswith("⚠️") and "уже открыт свой LONG BTCUSDT 0.03 BTC" in m.text
        assert labels(m) == ["✅ Вести как копию", "✖️ Не трогать"] and ad.hpos == {(BTC, "long"): 0.03}
    out = run(c.answer_offer(offer_id(m, False), False, rk(LONG4), 0.15))
    assert "не трогаю" in out[0].text and not c.syms


def test_adopted_position_keeps_size_and_follows_reduce_and_close():
    """Как просил Никита 25.11: свой LONG BTC остаётся своего размера, но сокращается и закрывается за Растом."""
    ad, c = hedge()
    ad.hpos[(BTC, "long")] = 0.03
    his = P(side="long", size=0.4, avg=57903.3, margin=463.23, tp=61222.0, sl=56665.0, known=True)
    msgs = run(c.resync(rk(his), 0.15, "🤖 Автомат включён. Сверка позиций:"))
    m = msgs[-1]
    assert labels(m) == ["✅ Вести как копию", "✖️ Не трогать"]
    out = run(c.answer_offer(offer_id(m), True, rk(his), 0.15))
    assert "Готово" in out[0].text and "0.03" in out[0].text
    assert c.syms[("BTCUSDT", "long")].expected == 0.03 and ad.hpos == {(BTC, "long"): 0.03}
    assert ad.stops[BTC] == {"tp": 61222.0, "sl": 56665.0, "amount": 0.03}       # его стопы на весь объём
    half = P(side="long", size=0.2, avg=57903.3, margin=231.6)
    run(c.on_event(Event("decrease", "BTCUSDT", his, half), None, auto=True))
    assert ad.hpos == {(BTC, "long"): 0.015} and ad.stops[BTC]["amount"] == 0.015
    run(c.on_event(Event("increase", "BTCUSDT", half, his), 0.15, auto=True))  # долил +0.2 → +0.2 × k
    assert ad.hpos == {(BTC, "long"): 0.045}
    run(c.on_event(Event("close", "BTCUSDT", his, None), None, auto=True))
    assert ad.hpos == {} and not c.syms


def test_detached_copy_can_be_followed_again():
    ad, c = hedge()
    run(c.on_event(Event("open", "BTCUSDT", None, P()), 0.2))              # копия 0.04
    ad.hpos[(BTC, "short")] = 0.06                                         # долил руками → отцепится
    run(c.on_event(Event("increase", "BTCUSDT", P(), P(size=0.3)), 0.2))
    assert c.syms[("BTCUSDT", "short")].detached
    (m,) = run(c.on_event(Event("tpsl", "BTCUSDT", P(size=0.3), P(size=0.3, tp=54321.0, known=True)), 0.2))
    assert labels(m) == ["✅ Вести как копию", "✖️ Не трогать"]
    run(c.answer_offer(offer_id(m), True, rk(P(size=0.3, tp=54321.0, known=True)), 0.2))
    st = c.syms[("BTCUSDT", "short")]
    assert not st.detached and st.expected == 0.06 and st.his_size == 0.3


def test_one_way_account_offers_only_close_and_enter():
    ad, c = make(price=58700.0)
    ad.positions[BTC] = ("short", 0.05)
    (m,) = run(c.on_event(Event("open", "BTCUSDT", None, LONG4), 0.15, auto=False))
    assert labels(m) == ["✅ Закрыть SHORT и войти в LONG", "✖️ Ничего"]
    run(c.answer_offer(offer_id(m), True, rk(LONG4), 0.15))
    assert ad.positions[BTC] == ("long", 0.06)


def test_conflict_also_on_reoffer_menu_and_resync():
    ad, c = hedge()
    ad.hpos[(BTC, "short")] = 0.05
    (m,) = run(c.on_event(Event("increase", "BTCUSDT", P(side="long", size=0.2), LONG4), 0.15, auto=False))
    assert labels(m)[0] == "✅ Закрыть SHORT и войти в LONG"
    (m,) = run(c.offer_entry(("BTCUSDT", "long"), LONG4, 0.15))
    assert labels(m)[0] == "✅ Закрыть SHORT и войти в LONG"
    msgs = run(c.resync(rk(LONG4), 0.15, "Сверка:"))
    assert labels(msgs[-1])[0] == "✅ Закрыть SHORT и войти в LONG"
    # «Ничего» — ничего не трогаем
    run(c.answer_offer(offer_id(msgs[-1], False), False, rk(LONG4), 0.15, choice="no"))
    assert ad.hpos == {(BTC, "short"): 0.05}


def test_hedged_rast_copies_are_not_treated_as_own():
    """Раст сам держит LONG и SHORT: наша копия шорта — не «твоя» позиция, конфликта нет."""
    ad, c = hedge()
    run(c.on_event(Event("open", "BTCUSDT", None, P(size=0.2)), 0.2))
    msgs = run(c.on_event(Event("open", "BTCUSDT", None, LONG4), 0.15))
    assert "открыл LONG" in msgs[0].text and not msgs[0].text.startswith("⚠️")


def adopted_long(sl=58500.0, size=0.0972):
    """Случай 26.11: свой LONG 0.0972 взят в копию его 0.4, стопы Раста TP 66 666 / SL 58 500."""
    ad, c = hedge(price=60170.0)
    ad.hpos[(BTC, "long")] = size
    his = P(side="long", size=0.4, avg=59045.6, margin=590.0, tp=66666.0, sl=sl, known=True)
    run(c._adopt(("BTCUSDT", "long"), his))
    return ad, c, his


def test_no_add_skips_his_additions_but_follows_reductions_and_close():
    ad, c, his = adopted_long()
    (m,) = c.set_guard(("BTCUSDT", "long"), no_add=True)
    assert "доливки Раста НЕ повторяю" in m.text
    for auto in (True, False):                           # и в автомате, и с подтверждением — без кнопок, без ордеров
        more = P(side="long", size=0.6, avg=59400.0, margin=890.0, tp=66666.0, sl=58500.0, known=True)
        (m,) = run(c.on_event(Event("increase", "BTCUSDT", his, more), 0.14, auto=auto))
        assert "не доливаю" in m.text and not m.buttons and ad.hpos[(BTC, "long")] == 0.0972
        c.syms[("BTCUSDT", "long")].his_size = 0.4
    c.syms[("BTCUSDT", "long")].his_size = 0.6
    half = P(side="long", size=0.3, avg=59400.0, margin=445.0, tp=66666.0, sl=58500.0, known=True)
    run(c.on_event(Event("decrease", "BTCUSDT", more, half), None, auto=True))      # сократил вдвое — и мы вдвое
    assert ad.hpos[(BTC, "long")] == 0.0486
    run(c.on_event(Event("close", "BTCUSDT", half, None), None, auto=True))
    assert ad.hpos == {} and not c.syms


def test_sl_only_tightens_for_long():
    ad, c, his = adopted_long()
    c.set_guard(("BTCUSDT", "long"), sl_tighten=True)
    key = ("BTCUSDT", "long")

    def moved(sl, tp=66666.0):
        return P(side="long", size=0.4, avg=59045.6, margin=590.0, tp=tp, sl=sl, known=True)
    (m,) = run(c.on_event(Event("tpsl", "BTCUSDT", his, moved(57800.0)), 0.14, auto=True))   # отодвинул вниз
    assert "SL Раста — 57 800, твой SL оставляю на 58 500" in m.text
    assert ad.stops[BTC]["sl"] == 58500.0 and c.syms[key].sl == 58500.0
    (m,) = run(c.on_event(Event("tpsl", "BTCUSDT", his, moved(59300.0)), 0.14, auto=True))   # подтянул вверх
    assert ad.stops[BTC]["sl"] == 59300.0 and "SL 59 300" in m.text
    (m,) = run(c.on_event(Event("tpsl", "BTCUSDT", his, moved(None, tp=70000.0)), 0.14, auto=True))  # убрал SL
    assert ad.stops[BTC] == {"tp": 70000.0, "sl": 59300.0, "amount": 0.0972}             # TP — за ним, SL держу
    assert "убрал SL" in m.text
    half = P(side="long", size=0.2, avg=59045.6, margin=295.0, tp=70000.0, sl=None, known=True)
    run(c.on_event(Event("decrease", "BTCUSDT", his, half), None, auto=True))          # перестановка после сокращения
    assert ad.stops[BTC] == {"tp": 70000.0, "sl": 59300.0, "amount": 0.0486}


def test_sl_only_tightens_for_short_and_guard_survives_restart():
    ad, c = hedge(price=59000.0)
    sh = P(side="short", size=0.4, avg=59345.0, margin=474.76, tp=55000.0, sl=60000.0, known=True)
    run(c.on_event(Event("open", "BTCUSDT", None, sh), 0.2, auto=True))
    c.set_guard(("BTCUSDT", "short"), sl_tighten=True, no_add=True)
    looser = P(side="short", size=0.4, avg=59345.0, margin=474.76, tp=55000.0, sl=60500.0, known=True)
    run(c.on_event(Event("tpsl", "BTCUSDT", sh, looser), 0.2, auto=True))
    assert ad.stops[BTC]["sl"] == 60000.0                          # для шорта «дальше» — это выше
    fresh = Copier(ad)
    fresh.restore(c.dump())
    st = fresh.syms[("BTCUSDT", "short")]
    assert st.no_add and st.sl_tighten and st.sl == 60000.0


def test_guard_needs_a_copy_and_readopt_cancels_old_stops():
    ad, c = hedge()
    assert "настраивать нечего" in c.set_guard(("BTCUSDT", "long"), no_add=True)[0].text
    ad, c, his = adopted_long()
    old_ids = list(c.syms[("BTCUSDT", "long")].stop_ids)
    c.syms[("BTCUSDT", "long")].detached = True                  # долил руками → отцепилась
    cancelled = []

    async def cancel(sym, ids):
        cancelled.extend(ids)
    ad.cancel_stops = cancel
    run(c._adopt(("BTCUSDT", "long"), his))
    assert cancelled[:len(old_ids)] == old_ids and old_ids



def test_own_sl_for_copy_then_only_tightens():
    """Случай 26.11: SL копии — 59 600 (как свой у Никиты), а не 58 500 Раста."""
    ad, c, his = adopted_long()
    key = ("BTCUSDT", "long")
    (m,) = run(c.set_sl(key, 59600.0))
    assert "SL LONG BTCUSDT → 59 600 на весь объём 0.0972" in m.text
    assert ad.stops[BTC] == {"tp": 66666.0, "sl": 59600.0, "amount": 0.0972} and c.syms[key].sl_tighten
    lower = P(side="long", size=0.4, avg=59045.6, margin=590.0, tp=66666.0, sl=59000.0, known=True)
    (m,) = run(c.on_event(Event("tpsl", "BTCUSDT", his, lower), 0.14, auto=True))
    assert ad.stops[BTC]["sl"] == 59600.0 and "оставляю на 59 600" in m.text
    higher = P(side="long", size=0.4, avg=59045.6, margin=590.0, tp=66666.0, sl=59900.0, known=True)
    run(c.on_event(Event("tpsl", "BTCUSDT", lower, higher), 0.14, auto=True))
    assert ad.stops[BTC]["sl"] == 59900.0
    (m,) = run(c.set_sl(key, 61000.0))                             # выше цены 60 170 — сработал бы сразу
    assert "сработал бы сразу" in m.text and ad.stops[BTC]["sl"] == 59900.0
    assert "ставить SL не на что" in run(c.set_sl(("BTCUSDT", "short"), 61000.0))[0].text
