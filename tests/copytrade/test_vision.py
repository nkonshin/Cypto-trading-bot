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
