"""Распознавание кадра стрима: вырезки → RapidOCR → разбор → Reading. Плюс детектор изменений панели."""
from __future__ import annotations

import time
from typing import Optional

import cv2
import numpy as np

from copytrade.model import Box, Pos, Reading
from copytrade.parse import (find_cards, parse_balance, parse_card, parse_tab, parse_tpsl, valid_tpsl)

REF_W, REF_H = 1720, 720
PANEL = (275, 428, 350, 140)      # вкладки + первая карточка до ряда средней цены
TPSL = (440, 566, 185, 18)        # строка «All Positions a ($)/b ($)» первой карточки
BALANCE = (275, 630, 200, 40)     # «Assets Value»
PANEL_SCALE, TPSL_SCALE, BAL_SCALE = 2, 4, 3
PAD = 16


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


class PanelReader:
    def __init__(self, ocr=None):
        if ocr is None:
            from rapidocr_onnxruntime import RapidOCR
            ocr = RapidOCR()
        self.ocr = ocr

    def _rect(self, frame, rect):
        h, w = frame.shape[:2]
        sx, sy = w / REF_W, h / REF_H
        x, y, rw, rh = rect
        return int(x * sx), int(y * sy), max(1, int(rw * sx)), max(1, int(rh * sy))

    def ocr_boxes(self, frame: np.ndarray, rect, scale: int) -> list:
        x, y, w, h = self._rect(frame, rect)
        res, _ = self.ocr(_prep(frame[y:y + h, x:x + w], scale))
        out = []
        for pts, text, conf in res or []:
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            out.append(Box((min(xs) - PAD) / scale, (min(ys) - PAD) / scale,
                           (max(xs) - min(xs)) / scale, (max(ys) - min(ys)) / scale, text, float(conf)))
        return out

    def read(self, frame: np.ndarray, with_balance: bool = False) -> Reading:
        now = time.time()
        balance = None
        if with_balance:
            balance = parse_balance(self.ocr_boxes(frame, BALANCE, BAL_SCALE))
        boxes = self.ocr_boxes(frame, PANEL, PANEL_SCALE)
        self.last_boxes = boxes
        n = parse_tab(boxes)
        if n is None:
            return Reading(False, (), None, balance, "панель позиций не видна", now)
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
        pos, why = parse_card(boxes, sym_box, symbol)
        if pos is None:
            return Reading(False, (), n, balance, why, now)
        px, py, _, _ = self._rect(frame, (PANEL[0] + sym_box.x - 2, PANEL[1] + sym_box.y + 11, 40, 11))
        side = side_by_color(frame, px, py)
        if side is None:
            return Reading(False, (), n, balance, "сторона не определена", now)
        tp = sl = None
        known = False
        tb = self.ocr_boxes(frame, TPSL, TPSL_SCALE)
        parsed = parse_tpsl(" ".join(b.text for b in sorted(tb, key=lambda b: b.x)))
        if parsed and all(b.conf >= 0.8 for b in tb) and valid_tpsl(side, pos.mark, *parsed):
            tp, sl = parsed
            known = True
        pos = Pos(pos.symbol, side, pos.size, pos.avg, pos.margin, pos.mark, tp, sl, known)
        return Reading(True, (pos,), n, balance, "", now)


class ChangeDetector:
    """Говорит, изменилась ли панель в зоне статичных полей (PnL и mark замаскированы)."""

    def __init__(self, diff: int = 25, min_pixels: int = 6):
        # шум сжатия между кадрами в статичных зонах — 0 px, смена одной цифры объёма — ~20 px
        self.diff = diff
        self.min_pixels = min_pixels
        self.ref = None

    @staticmethod
    def _sig(frame: np.ndarray) -> np.ndarray:
        h, w = frame.shape[:2]
        sx, sy = w / REF_W, h / REF_H
        x, y, cw, ch = PANEL[0], PANEL[1], PANEL[2], 190
        g = cv2.cvtColor(frame[int(y * sy):int((y + ch) * sy), int(x * sx):int((x + cw) * sx)], cv2.COLOR_BGR2GRAY)
        g = cv2.resize(g, (cw, ch)).astype(np.int16)
        g[45:80, 240:] = 0     # PnL
        g[75:140, 110:] = 0    # маржа/ratio/mark/liq
        g[136:155, 260:] = 0   # суммы в скобках у TP/SL
        return g

    def changed(self, frame: np.ndarray) -> bool:
        sig = self._sig(frame)
        if self.ref is None:
            return True
        return int((np.abs(sig - self.ref) > self.diff).sum()) >= self.min_pixels

    def remember(self, frame: np.ndarray) -> None:
        self.ref = self._sig(frame)
