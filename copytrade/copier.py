"""Логика копирования: событие Раста → ордера на нашей бирже.

- Открытие: его объём × k. Добавка: его добавка × k (текущий k).
- Сокращение: наш объём уменьшается в той же доле, что у него. Закрытие: закрываем всё.
- Переворот: закрыть и открыть заново. TP/SL: стоп-ордера на весь наш объём, при изменениях пересоздаются.
- Позиции, открытые до старта, по умолчанию не догоняем (ignored), пока он их не закроет.
- Если наша позиция разошлась с ожидаемой (сработал наш стоп, ручное вмешательство), монета «отцеплена»
  до его закрытия: мы её не трогаем, только закрываем остаток вместе с ним.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from copytrade.model import Event, Pos
from copytrade.sizing import fit_margin, floor_step

log = logging.getLogger("copier.core")


@dataclass
class SymState:
    side: Optional[str] = None
    expected: float = 0.0
    detached: bool = False
    stop_ids: list = field(default_factory=list)


def fmt(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{v:,.4f}".rstrip("0").rstrip(".").replace(",", " ")


class Copier:
    def __init__(self, adapter, default_leverage: int = 10, margin_frac: float = 0.95, max_leverage: int = 125):
        self.ex = adapter
        self.default_leverage = default_leverage
        self.max_leverage = max_leverage
        self.margin_frac = margin_frac
        self.syms: dict = {}           # raw symbol -> SymState
        self.ignored: set = set()
        self.dry = adapter.name == "paper"

    def _tag(self) -> str:
        return {"paper": "[тест] ", "demo": "[демо] "}.get(self.ex.name, "")

    def _st(self, raw: str) -> SymState:
        return self.syms.setdefault(raw, SymState())

    # ---------- события ----------
    async def on_baseline(self, positions, k: float, join_existing: bool) -> list:
        msgs = []
        self.ignored.clear()
        for p in positions:
            if join_existing:
                msgs += await self._open(p, k, late=True)
            else:
                self.ignored.add(p.symbol)
                msgs.append(f"{self._tag()}У Раста уже открыт {p.side.upper()} {p.symbol} {fmt(p.size)} "
                            f"по {fmt(p.avg)}. В уже открытые не вхожу, начну со следующей сделки.")
        return msgs

    async def on_event(self, ev: Event, k: float) -> list:
        s = ev.symbol
        if ev.kind == "open":
            if s in self.ignored:
                self.ignored.discard(s)
            return await self._open(ev.after, k)
        if ev.kind == "close":
            if s in self.ignored:
                self.ignored.discard(s)
                return [f"{self._tag()}Раст закрыл {s}, которую я не копировал. Дальше копирую всё."]
            return await self._close(s, ev.before)
        if ev.kind == "flip":
            msgs = []
            if s in self.ignored:
                self.ignored.discard(s)
            else:
                msgs += await self._close(s, ev.before)
            return msgs + await self._open(ev.after, k)
        if s in self.ignored:
            a, b = ev.before, ev.after
            what = (f"TP {fmt(b.tp)} / SL {fmt(b.sl)}" if ev.kind == "tpsl"
                    else f"{'добавил' if ev.kind == 'increase' else 'сократил'} {fmt(a.size)} → {fmt(b.size)}")
            return [f"{self._tag()}Раст: {s} {what} (эту позицию не копирую: открыта до старта)"]
        if ev.kind in ("increase", "decrease"):
            return await self._resize(ev, k)
        if ev.kind == "tpsl":
            return await self._tpsl(ev.after)
        return []

    async def close_all(self) -> list:
        msgs = []
        for raw in list(self.syms):
            msgs += await self._close(raw, None, manual=True)
        return msgs

    # ---------- действия ----------
    async def _open(self, p: Pos, k: float, late: bool = False) -> list:
        info = self.ex.market(p.symbol)
        his = f"Раст: {p.side.upper()} {p.symbol} {fmt(p.size)} по {fmt(p.avg)}"
        if info is None:
            return [f"{self._tag()}{his}\n→ монеты нет на BingX, пропускаю."]
        lev = min(p.leverage or self.default_leverage, info.max_leverage, self.max_leverage)
        amount = floor_step(p.size * k, info.step)
        if amount < info.min_amount:
            return [f"{self._tag()}{his}\n→ при k={k} объём {fmt(p.size * k)} меньше минимального лота "
                    f"{fmt(info.min_amount)}, пропускаю."]
        try:
            px = await self.ex.price(info.symbol)
            _, free = await self.ex.balance()
            amount, cut = fit_margin(amount, px, lev, free, info.step, self.margin_frac)
            if amount < info.min_amount:
                return [f"{self._tag()}{his}\n→ не хватает маржи (свободно {fmt(free)} USDT), пропускаю."]
            await self.ex.prepare(info.symbol, lev)
            fill = await self.ex.market_order(info.symbol, "buy" if p.side == "long" else "sell", amount, False)
        except Exception as e:
            log.exception("open %s", p.symbol)
            return [f"{self._tag()}{his}\n→ ОШИБКА открытия: {e}"]
        st = self._st(p.symbol)
        st.side, st.expected, st.detached = p.side, amount, False
        note = " (ужал под маржу)" if cut else ""
        note += " (вход с опозданием)" if late else ""
        msgs = [f"{self._tag()}{his} ({lev}x)\n→ открыл {p.side.upper()} {fmt(amount)} по ~{fmt(fill)}, "
                f"маржа ~{fmt(amount * fill / lev)} USDT{note}"]
        if p.tpsl_known:
            msgs += await self._tpsl(p)
        return msgs

    async def _close(self, raw: str, before: Optional[Pos], manual: bool = False) -> list:
        info = self.ex.market(raw)
        st = self.syms.get(raw)
        if info is None or st is None:
            return []
        head = "Закрываю копию" if manual else f"Раст закрыл {raw}"
        try:
            await self.ex.cancel_stops(info.symbol, st.stop_ids)
            side, amt = await self.ex.position(info.symbol)
            fill = None
            if amt > 0:
                fill = await self.ex.market_order(info.symbol, "sell" if side == "long" else "buy", amt, True)
        except Exception as e:
            log.exception("close %s", raw)
            return [f"{self._tag()}{head}\n→ ОШИБКА закрытия: {e}"]
        self.syms.pop(raw, None)
        if amt <= 0:
            return [f"{self._tag()}{head}\n→ у тебя позиции не было (уже закрыта стопом?)."]
        return [f"{self._tag()}{head}\n→ закрыл {(side or '').upper()} {fmt(amt)} по ~{fmt(fill)}"]

    async def _check_drift(self, raw: str, info) -> Optional[str]:
        st = self.syms.get(raw)
        if st is None or st.detached:
            return "detached"
        side, amt = await self.ex.position(info.symbol)
        if side != st.side or abs(amt - st.expected) > info.step * 1.5:
            st.detached = True
            return (f"{self._tag()}Твоя позиция {raw} разошлась с ожидаемой: на бирже {fmt(amt)} "
                    f"{(side or '').upper()}, ждал {fmt(st.expected)} {(st.side or '').upper()} "
                    f"(сработал стоп или ручное вмешательство). {raw} больше не трогаю, пока Раст её не закроет.")
        return None

    async def _resize(self, ev: Event, k: float) -> list:
        raw, a, b = ev.symbol, ev.before, ev.after
        info = self.ex.market(raw)
        if info is None or raw not in self.syms:
            return []
        try:
            drift = await self._check_drift(raw, info)
            if drift:
                return [] if drift == "detached" else [drift]
            st = self.syms[raw]
            his = f"Раст: {ev.kind == 'increase' and 'добавил' or 'сократил'} {raw} {fmt(a.size)} → {fmt(b.size)}"
            if ev.kind == "increase":
                add = floor_step((b.size - a.size) * k, info.step)
                if add < info.min_amount:
                    return [f"{self._tag()}{his}\n→ добавка {fmt((b.size - a.size) * k)} меньше лота, пропускаю."]
                px = await self.ex.price(info.symbol)
                _, free = await self.ex.balance()
                lev = min(b.leverage or a.leverage or self.default_leverage, info.max_leverage, self.max_leverage)
                add, cut = fit_margin(add, px, lev, free, info.step, self.margin_frac)
                if add < info.min_amount:
                    return [f"{self._tag()}{his}\n→ не хватает маржи на добавку, пропускаю."]
                fill = await self.ex.market_order(info.symbol, "buy" if st.side == "long" else "sell", add, False)
                st.expected = round(st.expected + add, 10)
                msg = f"{his}\n→ добавил {fmt(add)} по ~{fmt(fill)}, теперь {fmt(st.expected)}"
            else:
                target = floor_step(st.expected * b.size / a.size, info.step)
                cut_amt = floor_step(st.expected - target, info.step)
                if cut_amt < info.min_amount:
                    return [f"{self._tag()}{his}\n→ сокращение {fmt(st.expected - target)} меньше лота, оставляю."]
                fill = await self.ex.market_order(info.symbol, "sell" if st.side == "long" else "buy", cut_amt, True)
                st.expected = round(st.expected - cut_amt, 10)
                msg = f"{his}\n→ сократил на {fmt(cut_amt)} по ~{fmt(fill)}, осталось {fmt(st.expected)}"
            msgs = [self._tag() + msg]
            if b.tpsl_known or st.stop_ids:
                msgs += await self._tpsl(b, quiet=True)
            return msgs
        except Exception as e:
            log.exception("resize %s", raw)
            return [f"{self._tag()}Ошибка при изменении {raw}: {e}"]

    async def _tpsl(self, p: Pos, quiet: bool = False) -> list:
        raw = p.symbol
        info = self.ex.market(raw)
        st = self.syms.get(raw)
        if info is None or st is None or st.detached or st.expected <= 0:
            return []
        try:
            drift = await self._check_drift(raw, info)
            if drift:
                return [] if drift == "detached" else [drift]
            await self.ex.cancel_stops(info.symbol, st.stop_ids)
            st.stop_ids = []
            if p.tpsl_known and (p.tp or p.sl):
                st.stop_ids = await self.ex.set_stops(info.symbol, st.side, st.expected, p.tp, p.sl)
        except Exception as e:
            log.exception("tpsl %s", raw)
            return [f"{self._tag()}Ошибка при выставлении TP/SL {raw}: {e}"]
        if quiet:
            return []
        return [f"{self._tag()}Раст: TP/SL {raw} → TP {fmt(p.tp)} / SL {fmt(p.sl)}\n→ выставил у тебя так же "
                f"на {fmt(st.expected)}"]
