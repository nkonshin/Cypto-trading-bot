from copytrade.model import Pos, Reading
from copytrade.tracker import Tracker

BAD = Reading(False, reason="панель не видна", ts=1)


def R(*positions, ts=1.0):
    return Reading(True, tuple(positions), len(positions), None, "", ts)


def P(side="short", size=0.2, tp=None, sl=None, known=False, sym="BTCUSDT"):
    return Pos(sym, side, size, 59571.7, round(size * 59571.7 / 50, 4), 59086.1, tp, sl, known)   # маржа ∝ объёму


def kinds(events):
    return [e.kind for e in events]


def test_baseline_after_two_equal_readings():
    t = Tracker()
    assert t.update(R(P())) == []
    ev = t.update(R(P()))
    assert kinds(ev) == ["baseline"] and ev[0].positions[0].size == 0.2


def test_single_flicker_is_ignored():
    t = Tracker()
    t.update(R())
    t.update(R())                       # baseline: нет позиций
    assert t.update(R(P(size=0.3))) == []      # один кадр с ошибкой
    assert t.update(R()) == []
    assert t.update(R()) == []


def test_bad_reading_breaks_streak():
    t = Tracker()
    t.update(R()); t.update(R())
    t.update(R(P()))
    t.update(BAD)
    assert t.update(R(P())) == []
    assert kinds(t.update(R(P()))) == ["open"]


def test_open_increase_decrease_flip_close():
    t = Tracker()
    t.update(R()); t.update(R())
    seq = [
        (P(size=0.2), "open"),
        (P(size=0.3), "increase"),
        (P(size=0.1), "decrease"),
        (P(side="long", size=0.1), "flip"),
        (None, "close"),
    ]
    for pos, kind in seq:
        r = R(pos) if pos else R()
        assert t.update(r) == []
        ev = t.update(r)
        assert kinds(ev) == [kind], kind


def test_tpsl_confirmed_separately_and_unknown_skipped():
    t = Tracker()
    t.update(R(P())); t.update(R(P()))
    assert t.update(R(P(tp=54321.0, sl=59750.0, known=True))) == []
    assert t.update(R(P())) == []                                    # TP/SL не прочитан — пропуск, не сброс
    ev = t.update(R(P(tp=54321.0, sl=59750.0, known=True)))
    assert kinds(ev) == ["tpsl"] and (ev[0].after.tp, ev[0].after.sl) == (54321.0, 59750.0)
    # перенос стопа
    t.update(R(P(tp=54321.0, sl=59500.0, known=True)))
    ev = t.update(R(P(tp=54321.0, sl=59500.0, known=True)))
    assert kinds(ev) == ["tpsl"] and ev[0].after.sl == 59500.0


def test_tpsl_survives_resize_but_not_flip():
    t = Tracker()
    t.update(R(P())); t.update(R(P()))
    t.update(R(P(tp=54321.0, sl=59750.0, known=True))); t.update(R(P(tp=54321.0, sl=59750.0, known=True)))
    t.update(R(P(size=0.3))); t.update(R(P(size=0.3)))
    assert t.confirmed["BTCUSDT"].sl == 59750.0
    t.update(R(P(side="long", size=0.3))); t.update(R(P(side="long", size=0.3)))
    assert not t.confirmed["BTCUSDT"].tpsl_known


def test_blind_for():
    t = Tracker()
    assert t.blind_for(10) == float("inf")
    t.update(R(ts=100.0))
    assert t.blind_for(130.0) == 30.0
    t.update(BAD)
    assert t.blind_for(160.0) == 60.0



def test_size_change_without_margin_change_is_ocr_error():
    """Реальный случай 25.11: на экране 0.4, OCR дважды прочитал 0.6 при той же марже 486.17."""
    t = Tracker()
    real = Pos("BTCUSDT", "short", 0.4, 58967.2, 486.1696, 58600.0)
    t.update(R(real)); t.update(R(real))
    bad = Pos("BTCUSDT", "short", 0.6, 58967.2, 486.1696, 58600.0)
    assert t.update(R(bad)) == [] and t.update(R(bad)) == []
    assert "без изменения маржи" in t.last_reading.reason
    assert t.confirmed["BTCUSDT"].size == 0.4
