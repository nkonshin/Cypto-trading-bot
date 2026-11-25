"""Подтверждение распознанного состояния и события изменений.

Позиция определяется парой (монета, сторона): у Раста hedge-режим, LONG и SHORT по одной монете живут
одновременно и независимо. Набор позиций считается настоящим, если совпал в `confirm_n` годных чтениях подряд.
TP/SL подтверждаются отдельно: `confirm_n` одинаковых прочитанных значений подряд, непрочитанные пропускаются.
"""
from __future__ import annotations

import time
from dataclasses import replace
from typing import Optional

from copytrade.model import Event, Reading


def pkey(p) -> tuple:
    return (p.symbol, p.side)


def _key(positions) -> tuple:
    return tuple(sorted((p.symbol, p.side, round(p.size, 8)) for p in positions))


class Tracker:
    def __init__(self, confirm_n: int = 2):
        self.confirm_n = confirm_n
        self.confirmed: Optional[dict] = None        # (symbol, side) -> Pos
        self._cand_key = None
        self._cand_n = 0
        self._tpsl_cand: dict = {}                   # (symbol, side) -> ((tp, sl), n)
        self.last_ok: float = 0.0
        self.last_reading: Optional[Reading] = None

    def blind_for(self, now: Optional[float] = None) -> float:
        if not self.last_ok:
            return float("inf")
        return (now or time.time()) - self.last_ok

    def _size_changed_without_margin(self, r: Reading) -> bool:
        """Изолированная маржа: объём не может измениться, а маржа остаться прежней — значит, OCR ошибся."""
        if not self.confirmed:
            return False
        for p in r.positions:
            c = self.confirmed.get(pkey(p))
            if c is None or not (c.margin and p.margin):
                continue
            if abs(p.size - c.size) > 1e-9 and abs(p.margin - c.margin) <= c.margin * 0.002:
                return True
        return False

    def update(self, r: Reading) -> list:
        if r.ok and self._size_changed_without_margin(r):
            r = Reading(False, r.positions, r.n_tab, r.balance, "объём изменился без изменения маржи — ошибка OCR",
                        r.ts)
        self.last_reading = r
        if not r.ok:
            self._cand_key, self._cand_n = None, 0
            return []
        self.last_ok = r.ts or time.time()
        key = _key(r.positions)
        if key == self._cand_key:
            self._cand_n += 1
        else:
            self._cand_key, self._cand_n = key, 1
        events = []
        by_key = {pkey(p): p for p in r.positions}
        if self._cand_n >= self.confirm_n:
            if self.confirmed is None:
                self.confirmed = dict(by_key)
                events.append(Event("baseline", positions=tuple(by_key.values())))
            elif key != _key(self.confirmed.values()):
                events += self._diff(self.confirmed, by_key)
                old = self.confirmed
                self.confirmed = {}
                for k, p in by_key.items():
                    o = old.get(k)
                    if o is not None and o.tpsl_known:
                        p = replace(p, tp=o.tp, sl=o.sl, tpsl_known=True)
                    else:
                        p = replace(p, tp=None, sl=None, tpsl_known=False)
                    self.confirmed[k] = p
                self._tpsl_cand = {k: v for k, v in self._tpsl_cand.items() if k in by_key}
        if self.confirmed is not None:
            events += self._tpsl(by_key)
        return events

    @staticmethod
    def _diff(old: dict, new: dict) -> list:
        out = []
        for k in sorted(set(old) | set(new)):
            a, b = old.get(k), new.get(k)
            if a and not b:
                out.append(Event("close", k[0], a, None, side=k[1]))
            elif b and not a:
                out.append(Event("open", k[0], None, b, side=k[1]))
            elif b.size > a.size:
                out.append(Event("increase", k[0], a, b, side=k[1]))
            elif b.size < a.size:
                out.append(Event("decrease", k[0], a, b, side=k[1]))
        # сначала закрытия, потом открытия: так копия не упрётся в маржу при перевороте
        order = {"close": 0, "decrease": 1, "increase": 2, "open": 3}
        return sorted(out, key=lambda e: order[e.kind])

    def _tpsl(self, by_key: dict) -> list:
        out = []
        for k, p in by_key.items():
            cur = self.confirmed.get(k)
            if cur is None or not p.tpsl_known:
                continue
            val = (p.tp, p.sl)
            prev, n = self._tpsl_cand.get(k, (None, 0))
            n = n + 1 if prev == val else 1
            self._tpsl_cand[k] = (val, n)
            if n >= self.confirm_n and (not cur.tpsl_known or (cur.tp, cur.sl) != val):
                new = replace(cur, tp=p.tp, sl=p.sl, tpsl_known=True, mark=p.mark)
                self.confirmed[k] = new
                out.append(Event("tpsl", k[0], cur, new, side=k[1]))
        return out
