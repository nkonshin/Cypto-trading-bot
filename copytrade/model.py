"""Структуры данных копировщика."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import NamedTuple, Optional

LEVERAGES = (1, 2, 3, 5, 8, 10, 12, 15, 20, 25, 30, 40, 50, 60, 75, 100, 125)


class Msg(NamedTuple):
    """Сообщение в Telegram; buttons — ((надпись, callback_data), …)."""
    text: str
    buttons: tuple = ()


class Box(NamedTuple):
    """Распознанный текст в координатах вырезки (масштаб 1:1)."""
    x: float
    y: float
    w: float
    h: float
    text: str
    conf: float


@dataclass(frozen=True)
class Pos:
    """Позиция Раста, как она видна на стриме."""
    symbol: str                  # "BTCUSDT"
    side: str                    # "long" | "short"
    size: float                  # в монетах (Positions (BTC))
    avg: Optional[float] = None
    margin: Optional[float] = None
    mark: Optional[float] = None
    tp: Optional[float] = None
    sl: Optional[float] = None
    tpsl_known: bool = False     # False — строку TP/SL не удалось прочитать
    lev: Optional[int] = None    # плечо из формы ордера (если прочитано и сошлось с объёмом/маржой)

    @property
    def leverage(self) -> Optional[int]:
        """Плечо из номинала и маржи, привязанное к ближайшему стандартному значению.
        Не похоже ни на одно стандартное — значит, цифры прочитаны вразнобой: None (берём безопасное)."""
        if self.lev:
            return self.lev
        if not (self.avg and self.margin):
            return None
        raw = self.size * self.avg / self.margin
        best = min(LEVERAGES, key=lambda v: abs(v - raw))
        return best if abs(best - raw) <= best * 0.12 else None


@dataclass
class Reading:
    """Результат одного распознавания кадра."""
    ok: bool
    positions: tuple = ()
    n_tab: Optional[int] = None
    balance: Optional[float] = None
    reason: str = ""
    ts: float = 0.0


@dataclass(frozen=True)
class Event:
    kind: str                    # baseline | open | close | increase | decrease | tpsl
    symbol: str = ""
    before: Optional[Pos] = None
    after: Optional[Pos] = None
    positions: tuple = field(default=())   # для baseline
    side: str = ""               # позиция = (монета, сторона): в hedge-режиме LONG и SHORT независимы

    @property
    def key(self) -> tuple:
        side = self.side or (self.after or self.before).side
        return (self.symbol, side)
