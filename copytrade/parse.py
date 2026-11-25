"""Разбор распознанного текста панели позиции. Чистые функции, координаты вырезки 1:1.

Раскладка (1720×720, панель (275,428)): строка вкладок y≈4, карточка: монета y≈53, бейдж стороны +13,
подпись «Positions (BTC)» +30 и ряд значений [объём, маржа, ratio] под ней, «Average Position Price» +57
и ряд [средняя, mark, liq] под ней.
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

from copytrade.model import Box, Pos

NUM_CONF = 0.9
_NUM = re.compile(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?")
_TAB = re.compile(r"(?i)p\w{4,9}\s*\(\s*(\d{1,2})\s*\)")
_SYMBOL = re.compile(r"^[A-Z0-9]{2,15}USDT$")
_UNIT = re.compile(r"(?i)ion\w*\s*\(\s*([A-Za-z0-9]{2,10})\s*\)")
# цена TP/SL — число прямо перед своей суммой «($…)», либо «--», если не задано
_TPSL_TOKEN = re.compile(r"(\d[\d.,]*)(?=\(\$)|(?<![\d.,$])--(?![\d])")


def norm(text: str) -> str:
    return text.replace("（", "(").replace("）", ")").replace("，", ",").strip()


def parse_number(text: str) -> Optional[float]:
    """Число из начала текста в строгом формате (59,571.7 / 0.2000). «54,3210» — None."""
    token = norm(text).split(" ")[0] if text else ""
    token = re.sub(r"^[^\d]+", "", token)
    token = re.sub(r"[^\d.,]+.*$", "", token)
    if not token or not _NUM.fullmatch(token):
        return None
    if re.match(r"^0\d", token):          # «04000» — потерянная точка, не 4000
        return None
    return float(token.replace(",", ""))


def parse_price(token: str, mark: Optional[float] = None) -> Optional[float]:
    """Строго, как parse_number. Если OCR испортил разделители («59.600.0», «59.6000»), берём только цифры
    и ставим десятичную точку на 0/1/2 знака: соседние трактовки отличаются в 10 раз, поэтому в коридор
    ±50% от рынка попадает максимум одна — её и берём."""
    strict = parse_number(token)
    if mark is None:
        return strict
    band = lambda v: 0.5 * mark < v < 1.5 * mark
    if strict is not None and band(strict):
        return strict
    digits = re.sub(r"\D", "", token)
    if not digits:
        return None
    ok = [int(digits) / 10 ** d for d in (0, 1, 2) if band(int(digits) / 10 ** d)]
    return ok[0] if len(ok) == 1 else None


def parse_tpsl(text: str, mark: Optional[float] = None):
    """«54,321.0($1,050.14)/59,750.0($35.66)» → (54321.0, 59750.0); «--/--» → (None, None); мусор → None."""
    t = norm(text).replace(" ", "")
    tokens = [m.group(1) or "--" for m in _TPSL_TOKEN.finditer(t)]
    if len(tokens) != 2:
        return None
    out = []
    for tok in tokens:
        if tok == "--":
            out.append(None)
            continue
        v = parse_price(tok, mark)
        if v is None:
            return None
        out.append(v)
    return tuple(out)


def valid_tpsl(side: str, mark: Optional[float], tp: Optional[float], sl: Optional[float]) -> bool:
    """TP по ту сторону рынка, где прибыль, SL — где убыток (иначе ордер уже бы сработал)."""
    if mark is None:
        return False
    for v in (tp, sl):
        if v is not None and not (0.5 * mark < v < 1.5 * mark):
            return False
    if side == "short":
        return (tp is None or tp < mark) and (sl is None or sl > mark)
    return (tp is None or tp > mark) and (sl is None or sl < mark)


def _row_below(boxes: Iterable[Box], label: Box, dy=(5, 18)) -> list:
    return sorted((b for b in boxes if label.y + dy[0] <= b.y <= label.y + dy[1]), key=lambda b: b.x)


def _num_in(row, x_from, x_to, min_conf=NUM_CONF) -> Optional[float]:
    for b in row:
        if x_from <= b.x < x_to and b.conf >= min_conf:
            v = parse_number(b.text)
            if v is not None:
                return v
    return None


def parse_tab(boxes) -> Optional[int]:
    for b in boxes:
        if b.y < 20:
            m = _TAB.search(norm(b.text))
            if m:
                return int(m.group(1))
    return None


def find_cards(boxes) -> list:
    cards = []
    for b in boxes:
        t = norm(b.text).upper()
        if "ERPETUAL" in t or "PERP" in t:
            sym = re.sub(r"[^A-Z0-9]", "", t.split("PERP")[0] if "PERP" in t else t.split("ERPETUAL")[0])
            cards.append((b, sym))
    return sorted(cards, key=lambda c: c[0].y)


def parse_card(boxes, sym_box: Box, symbol: str):
    """(Pos без стороны/TP-SL | None, причина). Объём, средняя и маржа обязаны сходиться в стандартное плечо —
    это главная защита от ошибок OCR («0.4000» → «04000»)."""
    if not _SYMBOL.match(symbol):
        return None, f"монета не распознана: {symbol!r}"
    base = symbol[:-4]
    cy = sym_box.y
    size_label = next((b for b in boxes if cy + 20 <= b.y <= cy + 40 and b.x < 60), None)
    if size_label is None:
        return None, "нет подписи объёма"
    unit = re.sub(r"[^A-Z0-9]", "", norm(size_label.text).upper().split("(")[-1])
    if base not in unit and ("CONT" in unit or "USDT" in unit or "张" in size_label.text):
        return None, f"объём не в монетах ({unit})"
    row = _row_below(boxes, size_label)
    size = _num_in(row, 0, 60)
    margin = _num_in(row, 90, 200, 0.85)
    avg_label = next((b for b in boxes if cy + 45 <= b.y <= cy + 70 and b.x < 60 and "aver" in b.text.lower()), None)
    if avg_label is None:
        return None, "нет подписи средней цены"
    row2 = _row_below(boxes, avg_label)
    avg = _num_in(row2, 0, 60)
    mark = _num_in(row2, 90, 200, 0.85)
    if size is None or size <= 0:
        return None, "объём не прочитан"
    if avg is None:
        return None, "средняя цена не прочитана"
    if margin is None:
        return None, "маржа не прочитана"
    pos = Pos(symbol=symbol, side="", size=size, avg=avg, margin=margin, mark=mark)
    if pos.leverage is None:
        return None, f"объём/цена/маржа не сходятся ({size} × {avg} / {margin})"
    if mark is not None and not (0.7 * avg < mark < 1.3 * avg):
        mark = None
        pos = Pos(symbol=symbol, side="", size=size, avg=avg, margin=margin, mark=None)
    return pos, ""


def parse_balance(boxes) -> Optional[float]:
    for b in boxes:
        t = norm(b.text).replace(" ", "")
        if "USDT" in t.upper() and b.conf >= NUM_CONF:
            v = parse_number(t.upper().split("USDT")[0])
            if v is not None:
                return v
    return None
