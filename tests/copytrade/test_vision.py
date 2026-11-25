"""Распознавание настоящих кадров стрима (RapidOCR)."""
import os

import cv2
import pytest

pytest.importorskip("rapidocr_onnxruntime")

from copytrade.vision import ChangeDetector, PanelReader, side_by_color  # noqa: E402

FX = os.path.join(os.path.dirname(__file__), "fixtures")


@pytest.fixture(scope="module")
def reader():
    return PanelReader()


@pytest.mark.parametrize("name,mark,balance", [("short_btc_a", 59086.1, 748.7413), ("short_btc_b", 59077.7, 752.9813)])
def test_real_short_btc(reader, name, mark, balance):
    r = reader.read(cv2.imread(os.path.join(FX, name + ".jpg")), with_balance=True)
    assert r.ok, r.reason
    assert r.n_tab == 1
    assert r.balance == balance
    (p,) = r.positions
    assert (p.symbol, p.side, p.size, p.avg) == ("BTCUSDT", "short", 0.2, 59571.7)
    assert p.mark == mark
    assert p.margin == 245.5788 and p.leverage == 50
    assert p.tpsl_known and (p.tp, p.sl) == (54321.0, 59750.0)


def test_panel_shifted_by_limit_order_form(reader):
    """Раст переключил форму на лимитный ордер — панель уехала на 9 px вниз, SL перенесён на 59 600."""
    r = reader.read(cv2.imread(os.path.join(FX, "short_btc_04_limitform.jpg")), with_balance=True)
    assert r.ok, r.reason
    (p,) = r.positions
    assert (p.symbol, p.side, p.size, p.avg, p.leverage) == ("BTCUSDT", "short", 0.4, 59345.0, 50)
    assert p.tpsl_known and (p.tp, p.sl) == (54321.0, 59600.0)
    assert r.balance == 815.6503


GOLDEN = {   # кадр → (сторона, объём, средняя) — что реально было на экране
    "short_btc_a": ("short", 0.2, 59571.7),
    "short_btc_b": ("short", 0.2, 59571.7),
    "short_btc_04_limitform": ("short", 0.4, 59345.0),
    "srv_short_04_zero_glitch": ("short", 0.4, 59345.0),    # на сервере OCR прочитал объём как «04000»
    "srv_short_04_unit_glitch": ("short", 0.4, 59345.0),    # подпись «Poions 0BTC)»
    "srv_short_04_usot_glitch": ("short", 0.4, 59345.0),    # монета «BTCUSOT»
    "srv_short_04_lowconf": ("short", 0.4, 58967.2),        # объём «0.4000» с conf 0.84 — бот слеп 24 мин
}


@pytest.mark.parametrize("name", sorted(GOLDEN))
def test_never_wrong_on_real_frames(reader, name):
    """Главная гарантия: кадр можно не прочитать (бот подождёт следующий), но нельзя прочитать неверно."""
    r = reader.read(cv2.imread(os.path.join(FX, name + ".jpg")))
    if r.ok:
        (p,) = r.positions
        assert (p.side, p.size, p.avg) == GOLDEN[name]
        if p.tpsl_known:
            assert p.tp == 54321.0 and p.sl in (59750.0, 59600.0, 59400.0)


def test_low_confidence_size_now_read(reader):
    r = reader.read(cv2.imread(os.path.join(FX, "srv_short_04_lowconf.jpg")))
    assert r.ok, r.reason


def test_anchor_rejects_ad_frame():
    from copytrade.vision import locate
    ad = cv2.resize(cv2.imread(os.path.join(FX, "ad_break.jpg")), (1720, 720))
    assert locate(ad) is None
    assert PanelReader.read(PanelReader(ocr=lambda img: ([], 0)), ad).reason == "панель позиций не видна"


def test_side_color_on_real_frame():
    f = cv2.imread(os.path.join(FX, "short_btc_a.jpg"))
    assert side_by_color(f, 281, 492) == "short"
    assert side_by_color(f, 540, 490, 85, 12) == "long"    # зелёный PnL — как бейдж Long


def test_change_detector_ignores_pnl_and_mark():
    a = cv2.imread(os.path.join(FX, "short_btc_a.jpg"))
    b = cv2.imread(os.path.join(FX, "short_btc_b.jpg"))
    d = ChangeDetector()
    assert d.changed(a)
    d.remember(a)
    assert not d.changed(b)            # отличаются только PnL, mark и ratio
    c = a.copy()
    c[517:528, 291:297] = a[517:528, 305:311]   # 0.2000 → 0.0000: сменилась одна цифра
    assert d.changed(c)
    n = a.copy()                                # шум сжатия живого стрима: одиночные пиксели
    for y, x in ((457, 300), (457, 330), (457, 352), (484, 318), (484, 340), (530, 400)):
        n[y, x] = 255 - n[y, x]
    assert not d.changed(n)
