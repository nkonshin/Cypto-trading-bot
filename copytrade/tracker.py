"""Подтверждение распознанного состояния и события изменений.

Набор позиций (монета, сторона, объём) считается настоящим, если совпал в `confirm_n` годных чтениях подряд.
TP/SL подтверждаются отдельно: `confirm_n` одинаковых прочитанных значений подряд, непрочитанные пропускаются.
"""
from __future__ import annotations

import time
from dataclasses import replace
from typing import Optional

from copytrade.model import Event, Pos, Reading


def _key(positions) -> tuple:
    return tuple(sorted((p.symbol, p.side, round(p.size, 8)) for p in positions))


class Tracker:
    def __init__(self, confirm_n: int = 2):
        self.confirm_n = confirm_n
        self.confirmed: Optional[dict] = None        # symbol -> Pos
        self._cand_key = None
        self._cand_n = 0
        self._tpsl_cand: dict = {}                   # symbol -> ((tp, sl), n)
        self.last_ok: float = 0.0
        self.last_reading: Optional[Reading] = None

    def blind_for(self, now: Optional[float] = None) -> float:
        if not self.last_ok:
            return float("inf")
        return (now or time.time()) - self.last_ok

    def update(self, r: Reading) -> list:
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
        by_sym = {p.symbol: p for p in r.positions}
        if self._cand_n >= self.confirm_n:
            if self.confirmed is None:
                self.confirmed = dict(by_sym)
                events.append(Event("baseline", positions=tuple(by_sym.values())))
            elif key != _key(self.confirmed.values()):
                events += self._diff(self.confirmed, by_sym)
                keep_tpsl = {s: p for s, p in self.confirmed.items()}
                self.confirmed = {}
                for s, p in by_sym.items():
                    old = keep_tpsl.get(s)
                    if old is not None and old.side == p.side and old.tpsl_known:
                        p = replace(p, tp=old.tp, sl=old.sl, tpsl_known=True)
                    else:
                        p = replace(p, tp=None, sl=None, tpsl_known=False)
                    self.confirmed[s] = p
                self._tpsl_cand = {s: v for s, v in self._tpsl_cand.items() if s in by_sym}
        if self.confirmed is not None:
            events += self._tpsl(by_sym)
        return events

    @staticmethod
    def _diff(old: dict, new: dict) -> list:
        out = []
        for s in sorted(set(old) | set(new)):
            a, b = old.get(s), new.get(s)
            if a and not b:
                out.append(Event("close", s, a, None))
            elif b and not a:
                out.append(Event("open", s, None, b))
            elif a.side != b.side:
                out.append(Event("flip", s, a, b))
            elif b.size > a.size:
                out.append(Event("increase", s, a, b))
            elif b.size < a.size:
                out.append(Event("decrease", s, a, b))
        return out

    def _tpsl(self, by_sym: dict) -> list:
        out = []
        for s, p in by_sym.items():
            cur = self.confirmed.get(s)
            if cur is None or not p.tpsl_known or cur.side != p.side:
                continue
            val = (p.tp, p.sl)
            prev, n = self._tpsl_cand.get(s, (None, 0))
            n = n + 1 if prev == val else 1
            self._tpsl_cand[s] = (val, n)
            if n >= self.confirm_n and (not cur.tpsl_known or (cur.tp, cur.sl) != val):
                new = replace(cur, tp=p.tp, sl=p.sl, tpsl_known=True, mark=p.mark)
                self.confirmed[s] = new
                out.append(Event("tpsl", s, cur, new))
        return out
