"""Распознавание кадра стрима: вырезки → RapidOCR → разбор → Reading. Плюс детектор изменений панели."""
from __future__ import annotations

import os
import time
from typing import Optional

import cv2
import numpy as np

from copytrade.model import Box, Pos, Reading
from copytrade.parse import (find_cards, parse_balance, parse_card, parse_form, parse_number, parse_tab,
                             parse_tpsl, valid_tpsl)

REF_W, REF_H = 1720, 720
# Координаты в эталонной раскладке (кадр short_btc_a). Панель «плавает» по вертикали, когда Раст меняет
# форму ордера (лимитный ордер добавляет строки), поэтому всё отсчитывается от якоря — надписи вкладок.
ANCHOR_AT = (395, 431)                       # где якорь стоит в эталоне
ANCHOR_SEARCH = (250, 340, 410, 260)         # где его искать
ANCHOR_MIN_SCORE = 0.8
PANEL = (275, 428, 350, 140)      # вкладки + первая карточка до ряда средней цены (эталон)
TPSL_DX, TPSL_DY, TPSL_W, TPSL_H = 165, 84, 185, 18   # строка TP/SL от левого края панели / от строки монеты
BALANCE = (275, 622, 200, 50)     # «Assets Value» (не двигается)
FORM_HEADER = (280, 95, 120, 16)  # «BTCUSDT Perpetual» над формой ордера (не двигается)
FORM_LEV = (420, 145, 200, 25)    # «Isolated  50X» в форме ордера (не двигается)
SIZE_DX, SIZE_DY, SIZE_W, SIZE_H = 0, 38, 70, 16       # значение объёма от левого края панели / строки монеты
PANEL_SCALE, TPSL_SCALE, BAL_SCALE = 2, 4, 3
PAD = 16
_TEMPLATE = cv2.imread(os.path.join(os.path.dirname(__file__), "assets", "tabs_anchor.png"), cv2.IMREAD_GRAYSCALE)


def to_ref(frame: np.ndarray) -> np.ndarray:
    h, w = frame.shape[:2]
    return frame if (w, h) == (REF_W, REF_H) else cv2.resize(frame, (REF_W, REF_H), interpolation=cv2.INTER_AREA)


def locate(frame: np.ndarray):
    """Сдвиг панели относительно эталона: (dx, dy) или None, если панели не видно."""
    x, y, w, h = ANCHOR_SEARCH
    g = cv2.cvtColor(frame[y:y + h, x:x + w], cv2.COLOR_BGR2GRAY)
    res = cv2.matchTemplate(g, _TEMPLATE, cv2.TM_CCOEFF_NORMED)
    _, score, _, loc = cv2.minMaxLoc(res)
    if score < ANCHOR_MIN_SCORE:
        return None
    return loc[0] + x - ANCHOR_AT[0], loc[1] + y - ANCHOR_AT[1]


def _prep(crop: np.ndarray, scale: int) -> np.ndarray:
    """Увеличить, взять максимум по каналам (цветной текст светлеет), нормализовать, инвертировать."""
    c = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    g = cv2.normalize(c.max(axis=2), None, 0, 255, cv2.NORM_MINMAX)
    g = cv2.cvtColor(255 - g, cv2.COLOR_GRAY2BGR)
    return cv2.copyMakeBorder(g, PAD, PAD, PAD, PAD, cv2.BORDER_REPLICATE)


def side_by_color(frame: np.ndarray, x: int, y: int, w: int = 40, h: int = 11) -> Optional[str]:
    """Бейдж «Open Short» красный, «Open Long» зелёный."""
    px = frame[max(0, y):y + h, max(0, x):x + w].reshape(-1, 3).astype(int)
    px = px[px.max(axis=1) > 90]
    if len(px) < 15:
        return None
    b, g, r = px.mean(axis=0)
    if r > 1.6 * g and r > 70:
        return "short"
    if g > 1.3 * r and g > 70:
        return "long"
    return None


FORM_TTL = 60.0   # форма ордера меняется редко — читаем её не чаще раза в минуту (экономия CPU)


class PanelReader:
    def __init__(self, ocr=None):
        if ocr is None:
            from rapidocr_onnxruntime import RapidOCR
            ocr = RapidOCR()
        self.ocr = ocr
        self._form = (None, None)
        self._form_at = 0.0

    def form(self, frame, force: bool = False):
        """(монета, плечо) из формы ордера, с кешем на FORM_TTL."""
        if force or time.time() - self._form_at > FORM_TTL or self._form == (None, None):
            self._form = parse_form(self.ocr_boxes(frame, FORM_HEADER, 2), self.ocr_boxes(frame, FORM_LEV, 2))
            self._form_at = time.time()
        return self._form

    def ocr_boxes(self, frame: np.ndarray, rect, scale: int) -> list:
        x, y, w, h = (int(v) for v in rect)
        res, _ = self.ocr(_prep(frame[max(0, y):y + h, max(0, x):x + w], scale))
        out = []
        for pts, text, conf in res or []:
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            out.append(Box((min(xs) - PAD) / scale, (min(ys) - PAD) / scale,
                           (max(xs) - min(xs)) / scale, (max(ys) - min(ys)) / scale, text, float(conf)))
        return out

    def read(self, frame: np.ndarray, with_balance: bool = False) -> Reading:
        now = time.time()
        frame = to_ref(frame)
        balance = None
        if with_balance:
            balance = parse_balance(self.ocr_boxes(frame, BALANCE, BAL_SCALE))
        off = locate(frame)
        if off is None:
            return Reading(False, (), None, balance, "панель позиций не видна", now)
        px0, py0 = PANEL[0] + off[0], PANEL[1] + off[1]
        boxes = self.ocr_boxes(frame, (px0, py0, PANEL[2], PANEL[3]), PANEL_SCALE)
        self.last_boxes = boxes
        n = parse_tab(boxes)
        if n is None:
            return Reading(False, (), None, balance, "вкладка позиций не прочитана", now)
        cards = find_cards(boxes)
        if n == 0:
            if cards:
                return Reading(False, (), n, balance, "вкладка 0, но карточка видна", now)
            return Reading(True, (), 0, balance, "", now)
        if n > 1:
            return Reading(False, (), n, balance, f"у него {n} позиций, вижу только первую", now)
        if len(cards) != 1:
            return Reading(False, (), n, balance, "карточка позиции не найдена", now)
        sym_box, symbol = cards[0]
        # объём ещё трижды — отдельной вырезкой ×3/×4/×5; вместе с панелью это голосование (нужно ≥2 совпадения)
        rect = (px0 + SIZE_DX, py0 + sym_box.y + SIZE_DY, SIZE_W, SIZE_H)
        votes = []
        for sc in (3, 4, 5):
            sb = self.ocr_boxes(frame, rect, sc)
            votes.append(next((parse_number(b.text) for b in sb if b.conf >= 0.85 and parse_number(b.text)), None))
        self.last_votes = votes
        form_sym, form_lev = self.form(frame)
        pos, why = parse_card(boxes, sym_box, symbol, None, form_lev if form_sym == symbol else None, votes)
        if pos is None and "плечом" in why:            # вдруг он поменял плечо — перечитать форму сразу
            form_sym, form_lev = self.form(frame, force=True)
            pos, why = parse_card(boxes, sym_box, symbol, None, form_lev if form_sym == symbol else None, votes)
        if pos is None:
            return Reading(False, (), n, balance, why, now)
        side = side_by_color(frame, int(px0 + sym_box.x - 2), int(py0 + sym_box.y + 11))
        if side is None:
            return Reading(False, (), n, balance, "сторона не определена", now)
        tp = sl = None
        known = False
        tb = self.ocr_boxes(frame, (px0 + TPSL_DX, py0 + sym_box.y + TPSL_DY, TPSL_W, TPSL_H), TPSL_SCALE)
        parsed = parse_tpsl(" ".join(b.text for b in sorted(tb, key=lambda b: b.x)), pos.mark)
        if parsed and all(b.conf >= 0.8 for b in tb) and valid_tpsl(side, pos.mark, *parsed):
            tp, sl = parsed
            known = True
        pos = Pos(pos.symbol, side, pos.size, pos.avg, pos.margin, pos.mark, tp, sl, known, pos.lev)
        return Reading(True, (pos,), n, balance, "", now)


class ChangeDetector:
    """Говорит, изменилась ли панель в зоне статичных полей (PnL и mark замаскированы)."""

    def __init__(self, diff: int = 25, min_pixels: int = 5):
        # на живом стриме шум сжатия — 1–6 одиночных пикселей; считаем только «кластеры» (≥3 изменённых
        # в окне 3×3): шум → 0, смена одной цифры объёма → ~15 px
        self.diff = diff
        self.min_pixels = min_pixels
        self.ref = None

    @staticmethod
    def _sig(frame: np.ndarray) -> np.ndarray:
        frame = to_ref(frame)
        dx, dy = locate(frame) or (0, 0)
        x, y, cw, ch = PANEL[0] + dx, max(0, PANEL[1] + dy), PANEL[2], 190
        g = cv2.cvtColor(frame[y:y + ch, x:x + cw], cv2.COLOR_BGR2GRAY)
        g = cv2.resize(g, (cw, ch)).astype(np.int16)
        g[45:80, 240:] = 0     # PnL
        g[75:140, 110:] = 0    # маржа/ratio/mark/liq
        g[136:155, 260:] = 0   # суммы в скобках у TP/SL
        return g

    def changed(self, frame: np.ndarray) -> bool:
        sig = self._sig(frame)
        if self.ref is None:
            return True
        d = (np.abs(sig - self.ref) > self.diff).astype(np.float32)
        clustered = (cv2.boxFilter(d, -1, (3, 3), normalize=False) >= 3) & (d > 0)
        return int(clustered.sum()) >= self.min_pixels

    def remember(self, frame: np.ndarray) -> None:
        self.ref = self._sig(frame)
