"""Распознавание кадра стрима: вырезки → RapidOCR → разбор → Reading. Плюс детектор изменений панели."""
from __future__ import annotations

import os
import time
from typing import Optional

import cv2
import numpy as np

from copytrade.model import Box, Pos, Reading
from copytrade.parse import (find_cards, parse_balance, parse_card, parse_form, parse_number, parse_tab,
                             parse_tpsl, valid_tpsl, vote)

REF_W, REF_H = 1720, 720
# Координаты в эталонной раскладке (кадр short_btc_a). Панель «плавает» по вертикали, когда Раст меняет
# форму ордера (лимитный ордер добавляет строки), поэтому всё отсчитывается от якоря — надписи вкладок.
ANCHOR_AT = (395, 431)                       # где якорь стоит в эталоне
ANCHOR_SEARCH = (250, 60, 410, 600)          # где его искать: вся левая колонка (форма бывает разной высоты)
CARD_PITCH = 149                             # шаг карточек позиций по вертикали
CARD_CHUNK_H = 90                            # вырезка следующей карточки: строка монеты … ряд средней цены
PANEL_BOTTOM = 625                           # ниже — «Assets Value», карточки туда не помещаются
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

    def _size_votes(self, frame, rect, panel_size):
        """Дополнительные чтения объёма (×4, ×3, ×5) — до первых двух совпадений вместе с панелью."""
        votes = []
        for sc in (4, 3, 5):
            sb = self.ocr_boxes(frame, rect, sc)
            votes.append(next((parse_number(b.text) for b in sb if b.conf >= 0.85 and parse_number(b.text)), None))
            if vote([panel_size] + votes) is not None:
                break
        return votes

    def _read_card(self, frame, cx, cy, boxes, sym_box, symbol, known=None):
        """Одна карточка позиции; (cx, cy) — левый верхний угол вырезки, в которой лежат boxes.
        known — уже подтверждённые позиции: если объём и маржа не изменились, лишние чтения объёма не нужны."""
        form_sym, form_lev = self.form(frame)
        flev = form_lev if form_sym == symbol else None
        side = side_by_color(frame, int(cx + sym_box.x - 2), int(cy + sym_box.y + 11))
        if side is None:
            return None, "сторона не определена"
        quick, _ = parse_card(boxes, sym_box, symbol, None, flev)
        k = (known or {}).get((symbol, side))
        if (quick is not None and k is not None and abs(quick.size - k.size) < 1e-9 and k.margin
                and abs(quick.margin - k.margin) <= k.margin * 0.002):
            pos, why = quick, ""
            self.last_votes = "не нужны: позиция не менялась"
        else:
            panel_size = parse_card(boxes, sym_box, symbol, None, None)[0]
            rect = (cx + SIZE_DX, cy + sym_box.y + SIZE_DY, SIZE_W, SIZE_H)
            votes = self._size_votes(frame, rect, panel_size.size if panel_size else None)
            self.last_votes = votes
            pos, why = parse_card(boxes, sym_box, symbol, None, flev, votes)
            if pos is None and "плечом" in why:        # вдруг он поменял плечо — перечитать форму сразу
                form_sym, form_lev = self.form(frame, force=True)
                pos, why = parse_card(boxes, sym_box, symbol, None, form_lev if form_sym == symbol else None, votes)
        if pos is None:
            return None, why
        tp = sl = None
        known = False
        tb = self.ocr_boxes(frame, (cx + TPSL_DX, cy + sym_box.y + TPSL_DY, TPSL_W, TPSL_H), TPSL_SCALE)
        parsed = parse_tpsl(" ".join(b.text for b in sorted(tb, key=lambda b: b.x)), pos.mark)
        if parsed and all(b.conf >= 0.75 for b in tb) and valid_tpsl(side, pos.mark, *parsed):
            tp, sl = parsed
            known = True
        return Pos(pos.symbol, side, pos.size, pos.avg, pos.margin, pos.mark, tp, sl, known, pos.lev), ""

    def read(self, frame: np.ndarray, with_balance: bool = False, known: dict = None) -> Reading:
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
        if len(cards) != 1:
            return Reading(False, (), n, balance, "карточка позиции не найдена", now)
        sym_box, symbol = cards[0]
        pos, why = self._read_card(frame, px0, py0, boxes, sym_box, symbol, known)
        if pos is None:
            return Reading(False, (), n, balance, why, now)
        positions = [pos]
        for i in range(1, n):
            top = int(py0 + sym_box.y + i * CARD_PITCH - 8)
            if top + CARD_CHUNK_H > PANEL_BOTTOM:
                return Reading(False, (), n, balance, f"у него {n} позиций, на экране видно только {i}", now)
            cb = self.ocr_boxes(frame, (px0, top, PANEL[2], CARD_CHUNK_H), PANEL_SCALE)
            cs = find_cards(cb)
            if len(cs) != 1:
                return Reading(False, (), n, balance, f"карточка позиции №{i + 1} не найдена", now)
            p, why = self._read_card(frame, px0, top, cb, cs[0][0], cs[0][1], known)
            if p is None:
                return Reading(False, (), n, balance, f"позиция №{i + 1}: {why}", now)
            positions.append(p)
        if len({(p.symbol, p.side) for p in positions}) != len(positions):
            return Reading(False, (), n, balance, "две карточки с одной и той же позицией", now)
        return Reading(True, tuple(positions), n, balance, "", now)


class ChangeDetector:
    """Говорит, изменилась ли панель в зоне статичных полей (PnL и mark замаскированы)."""

    def __init__(self, diff: int = 25, min_pixels: int = 5):
        # на живом стриме шум сжатия — 1–6 одиночных пикселей; считаем только «кластеры» (≥3 изменённых
        # в окне 3×3): шум → 0, смена одной цифры объёма → ~15 px
        self.diff = diff
        self.min_pixels = min_pixels
        self.ref = None

    SIG_H = 360          # вкладки + до трёх карточек

    @staticmethod
    def _sig(frame: np.ndarray) -> np.ndarray:
        frame = to_ref(frame)
        dx, dy = locate(frame) or (0, 0)
        x, y, cw = PANEL[0] + dx, max(0, PANEL[1] + dy), PANEL[2]
        h = ChangeDetector.SIG_H
        g = np.zeros((h, cw), np.int16)
        bottom = min(y + h, PANEL_BOTTOM)                 # ниже — баланс и PnL дня, они меняются постоянно
        if bottom > y:
            g[:bottom - y] = cv2.cvtColor(frame[y:bottom, x:x + cw], cv2.COLOR_BGR2GRAY).astype(np.int16)
        for i in range(3):                                # динамичные поля каждой карточки
            o = i * CARD_PITCH
            g[max(0, 45 + o):max(0, 80 + o), 240:] = 0     # PnL
            g[max(0, 75 + o):max(0, 140 + o), 110:] = 0    # маржа/ratio/mark/liq
            g[max(0, 136 + o):max(0, 155 + o), 260:] = 0   # суммы в скобках у TP/SL
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
