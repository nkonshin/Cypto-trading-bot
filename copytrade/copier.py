"""Логика копирования: событие Раста → ордера на нашей бирже.

- Бот управляет только позициями, которые открыл сам (self.syms). Ручные позиции Никиты не трогает никогда.
- Монеты из auto_symbols (по умолчанию BTCUSDT) копируются сами; остальные — предложением с кнопками.
- Объём: его объём × k, где k считает App в момент сделки (наш баланс / его баланс × множитель).
- Добавка: его добавка × текущий k. Сокращение: наш объём уменьшается в той же доле. Закрытие: всё.
- Переворот: закрыть и открыть (или предложить). TP/SL: стоп-ордера на весь наш объём, пересоздаются.
- Если наша позиция разошлась с ожидаемой (сработал наш стоп, ручное вмешательство), монета «отцеплена»:
  бот её больше не трогает, пока Раст её не закроет; тогда снимает с учёта, ничего не закрывая.
- Позиции Раста, которые мы не копируем (открыты до старта, во время паузы, пропущены) — только сообщения.
"""
from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Optional

from copytrade.model import Event, Msg, Pos
from copytrade.sizing import fit_margin, floor_step

log = logging.getLogger("copier.core")
OFFER_TTL = 15 * 60


@dataclass
class SymState:
    side: Optional[str] = None
    expected: float = 0.0
    detached: bool = False
    stop_ids: list = field(default_factory=list)


@dataclass
class Offer:
    id: str
    kind: str            # "open" — войти в его позицию; "close" — закрыть нашу копию, которой у него уже нет
    symbol: str
    side: str
    created: float


def fmt(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{v:,.4f}".rstrip("0").rstrip(".").replace(",", " ")


def desc(p: Pos) -> str:
    lev = f" ({p.leverage}x)" if p.leverage else ""
    stops = f", TP {fmt(p.tp)} / SL {fmt(p.sl)}" if p.tpsl_known and (p.tp or p.sl) else ""
    return f"{p.side.upper()} {p.symbol} {fmt(p.size)} по {fmt(p.avg)}{lev}{stops}"


class Copier:
    def __init__(self, adapter, default_leverage: int = 10, margin_frac: float = 0.95, max_leverage: int = 125,
                 auto_symbols=("BTCUSDT",)):
        self.ex = adapter
        self.default_leverage = default_leverage
        self.max_leverage = max_leverage
        self.margin_frac = margin_frac
        self.auto_symbols = set(auto_symbols)
        self.syms: dict = {}           # raw symbol -> SymState (только наши копии)
        self.offers: dict = {}         # id -> Offer

    def _tag(self) -> str:
        return {"paper": "[тест] ", "demo": "[демо] "}.get(self.ex.name, "")

    def _m(self, text: str, buttons=()) -> Msg:
        return Msg(self._tag() + text, tuple(buttons))

    def dump(self) -> dict:
        """Наши копии — для сохранения на диск: после перезапуска бот должен помнить, что открывал он."""
        return {raw: {"side": st.side, "expected": st.expected, "detached": st.detached, "stop_ids": list(st.stop_ids)}
                for raw, st in self.syms.items()}

    def restore(self, data: dict) -> None:
        self.syms = {raw: SymState(v.get("side"), float(v.get("expected", 0)), bool(v.get("detached")),
                                   list(v.get("stop_ids") or [])) for raw, v in (data or {}).items()}

    def managed(self, raw: str) -> bool:
        st = self.syms.get(raw)
        return st is not None and not st.detached

    # ---------- предложения ----------
    def _offer(self, kind: str, p: Pos, text: str) -> Msg:
        for oid in [o.id for o in self.offers.values() if o.symbol == p.symbol]:
            self.offers.pop(oid, None)           # по монете живо только последнее предложение
        o = Offer(uuid.uuid4().hex[:10], kind, p.symbol, p.side, time.time())
        self.offers[o.id] = o
        if kind == "open":
            buttons = (("Открыть", f"copy:offer:{o.id}:yes"), ("Пропустить", f"copy:offer:{o.id}:no"))
        else:
            buttons = (("Закрыть копию", f"copy:offer:{o.id}:yes"), ("Оставить себе", f"copy:offer:{o.id}:no"))
        return self._m(text + "\n(кнопки действуют 15 мин)", buttons)

    def _drop_offers(self, raw: str) -> None:
        for oid in [o.id for o in self.offers.values() if o.symbol == raw]:
            self.offers.pop(oid, None)

    async def answer_offer(self, oid: str, yes: bool, rast: dict, k: float) -> list:
        """Ответ на кнопку. rast — текущие подтверждённые позиции Раста (symbol -> Pos)."""
        o = self.offers.pop(oid, None)
        if o is None or time.time() - o.created > OFFER_TTL:
            return [self._m("Это предложение уже неактуально.")]
        p = rast.get(o.symbol)
        if o.kind == "open":
            if not yes:
                return [self._m(f"Ок, {o.symbol} пропускаю.")]
            if p is None or p.side != o.side:
                return [self._m(f"Раст уже закрыл или перевернул {o.symbol}, не открываю.")]
            if self.managed(o.symbol):
                return [self._m(f"{o.symbol} уже копируется.")]
            return await self._open(p, k, late=True)
        # close
        if yes:
            return await self._close(o.symbol, None, manual=True)
        self.syms.pop(o.symbol, None)
        return [self._m(f"Ок, {o.symbol} оставляю тебе, больше её не трогаю.")]

    # ---------- события ----------
    async def on_event(self, ev: Event, k: float) -> list:
        s, a, b = ev.symbol, ev.before, ev.after
        if ev.kind == "open":
            return await self._open_or_offer(b, k)
        if ev.kind == "close":
            self._drop_offers(s)
            st = self.syms.get(s)
            if st is not None and st.detached:
                self.syms.pop(s, None)
                return [self._m(f"Раст закрыл {s}. Твою позицию по {s} не трогаю (она была отцеплена).")]
            if st is None:
                return [self._m(f"Раст закрыл {desc(a)} — её не копировали.")]
            return await self._close(s, a)
        if ev.kind == "flip":
            msgs = await self._close(s, a) if self.managed(s) else []
            return msgs + await self._open_or_offer(b, k)
        if not self.managed(s):
            what = (f"TP {fmt(b.tp)} / SL {fmt(b.sl)}" if ev.kind == "tpsl"
                    else f"{'добавил' if ev.kind == 'increase' else 'сократил'} {fmt(a.size)} → {fmt(b.size)}")
            return [self._m(f"Раст: {s} {what} (эту позицию не копирую)")]
        if ev.kind in ("increase", "decrease"):
            return await self._resize(ev, k)
        if ev.kind == "tpsl":
            return await self._tpsl(b)
        return []

    async def _open_or_offer(self, p: Pos, k: float) -> list:
        if p.symbol in self.auto_symbols:
            return await self._open(p, k)
        return [self._offer("open", p, f"Раст открыл {desc(p)}.\nЭто не BTC — открывать копию сам не буду. "
                                       f"Открыть пропорционально по рынку?")]

    async def resync(self, rast: dict, k: float, reason: str) -> list:
        """Сверка после паузы/старта: что у Раста, что у нас; по расхождениям — предложения с кнопками."""
        msgs, lines = [], []
        for raw in list(self.syms):
            st = self.syms[raw]
            info = self.ex.market(raw)
            if st.detached or info is None:
                continue
            side, amt = await self.ex.position(info.symbol, st.side)
            if side != st.side or abs(amt - st.expected) > info.step * 1.5:
                self.syms.pop(raw, None)
                lines.append(f"{raw}: у тебя {fmt(amt)} вместо {fmt(st.expected)} — считаю твоей, не трогаю.")
        for raw, p in rast.items():
            if self.managed(raw) and self.syms[raw].side == p.side:
                lines.append(f"{raw}: копия на месте ({fmt(self.syms[raw].expected)}), продолжаю вести.")
            else:
                msgs.append(self._offer("open", p, f"У Раста открыт {desc(p)}, у тебя копии нет.\n"
                                                   f"Открыть пропорционально по рынку (вход с опозданием)?"))
        for raw in list(self.syms):
            if self.managed(raw) and raw not in rast:
                st = self.syms[raw]
                fake = Pos(raw, st.side, st.expected)
                msgs.append(self._offer("close", fake, f"Раст уже закрыл {raw}, а у тебя копия открыта "
                                                       f"({st.side.upper()} {fmt(st.expected)}). Закрыть?"))
        if not rast and not self.syms:
            lines.append("Позиций нет ни у Раста, ни у тебя — жду следующую сделку.")
        head = self._m(f"{reason}\n" + "\n".join(lines)) if lines else self._m(reason)
        return [head] + msgs

    async def close_all(self) -> list:
        msgs = []
        for raw in list(self.syms):
            if self.managed(raw):
                msgs += await self._close(raw, None, manual=True)
        return msgs

    # ---------- действия ----------
    async def _open(self, p: Pos, k: float, late: bool = False) -> list:
        info = self.ex.market(p.symbol)
        his = f"Раст: {desc(p)}"
        if info is None:
            return [self._m(f"{his}\n→ монеты нет на BingX, пропускаю.")]
        if k is None or k <= 0:
            return [self._m(f"{his}\n→ не знаю его баланс, не могу посчитать пропорцию. Пропускаю.")]
        lev = min(p.leverage or self.default_leverage, info.max_leverage, self.max_leverage)
        amount = floor_step(p.size * k, info.step)
        if amount < info.min_amount:
            return [self._m(f"{his}\n→ объём {fmt(p.size * k)} (k={k:.3f}) меньше минимального лота "
                            f"{fmt(info.min_amount)}, пропускаю.")]
        try:
            px = await self.ex.price(info.symbol)
            _, free = await self.ex.balance()
            amount, cut = fit_margin(amount, px, lev, free, info.step, self.margin_frac)
            if amount < info.min_amount:
                return [self._m(f"{his}\n→ не хватает маржи (свободно {fmt(free)} USDT), пропускаю.")]
            await self.ex.prepare(info.symbol, lev)
            fill = await self.ex.market_order(info.symbol, "buy" if p.side == "long" else "sell", amount, False,
                                              p.side)
        except Exception as e:
            log.exception("open %s", p.symbol)
            return [self._m(f"{his}\n→ ОШИБКА открытия: {e}")]
        st = self.syms[p.symbol] = SymState(p.side, amount)
        note = " (ужал под маржу)" if cut else ""
        note += " (вход с опозданием)" if late else ""
        msgs = [self._m(f"{his}\n→ открыл {p.side.upper()} {fmt(amount)} по ~{fmt(fill)} (k={k:.3f}, {lev}x), "
                        f"маржа ~{fmt(amount * fill / lev)} USDT{note}")]
        if p.tpsl_known and (p.tp or p.sl):
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
            side, amt = await self.ex.position(info.symbol, st.side)
            fill = None
            if amt > 0:
                fill = await self.ex.market_order(info.symbol, "sell" if side == "long" else "buy", amt, True, side)
        except Exception as e:
            log.exception("close %s", raw)
            return [self._m(f"{head}\n→ ОШИБКА закрытия: {e}")]
        self.syms.pop(raw, None)
        if amt <= 0:
            return [self._m(f"{head}\n→ у тебя копии уже не было (закрылась стопом?).")]
        return [self._m(f"{head}\n→ закрыл {(side or '').upper()} {fmt(amt)} по ~{fmt(fill)}")]

    async def _check_drift(self, raw: str, info) -> Optional[str]:
        st = self.syms.get(raw)
        if st is None or st.detached:
            return "detached"
        side, amt = await self.ex.position(info.symbol, st.side)
        if side != st.side or abs(amt - st.expected) > info.step * 1.5:
            st.detached = True
            return (f"Твоя копия {raw} разошлась с ожидаемой: на бирже {fmt(amt)} {(side or '').upper()}, "
                    f"ждал {fmt(st.expected)} {(st.side or '').upper()} (сработал стоп или ты поправил руками). "
                    f"{raw} больше не трогаю, пока Раст её не закроет.")
        return None

    async def _resize(self, ev: Event, k: float) -> list:
        raw, a, b = ev.symbol, ev.before, ev.after
        info = self.ex.market(raw)
        if info is None:
            return []
        try:
            drift = await self._check_drift(raw, info)
            if drift:
                return [] if drift == "detached" else [self._m(drift)]
            st = self.syms[raw]
            his = f"Раст {'добавил' if ev.kind == 'increase' else 'сократил'} {raw}: {fmt(a.size)} → {fmt(b.size)}"
            if ev.kind == "increase":
                if k is None or k <= 0:
                    return [self._m(f"{his}\n→ не знаю его баланс, добавку пропускаю.")]
                add = floor_step((b.size - a.size) * k, info.step)
                if add < info.min_amount:
                    return [self._m(f"{his}\n→ добавка {fmt((b.size - a.size) * k)} меньше лота, пропускаю.")]
                px = await self.ex.price(info.symbol)
                _, free = await self.ex.balance()
                lev = min(b.leverage or a.leverage or self.default_leverage, info.max_leverage, self.max_leverage)
                add, cut = fit_margin(add, px, lev, free, info.step, self.margin_frac)
                if add < info.min_amount:
                    return [self._m(f"{his}\n→ не хватает маржи на добавку, пропускаю.")]
                fill = await self.ex.market_order(info.symbol, "buy" if st.side == "long" else "sell", add, False,
                                                  st.side)
                st.expected = round(st.expected + add, 10)
                text = f"{his}\n→ добавил {fmt(add)} по ~{fmt(fill)}, теперь {fmt(st.expected)}"
            else:
                target = floor_step(st.expected * b.size / a.size, info.step)
                cut_amt = floor_step(st.expected - target, info.step)
                if cut_amt < info.min_amount:
                    return [self._m(f"{his}\n→ сокращение {fmt(st.expected - target)} меньше лота, оставляю.")]
                fill = await self.ex.market_order(info.symbol, "sell" if st.side == "long" else "buy", cut_amt,
                                                  True, st.side)
                st.expected = round(st.expected - cut_amt, 10)
                text = f"{his}\n→ сократил на {fmt(cut_amt)} по ~{fmt(fill)}, осталось {fmt(st.expected)}"
            msgs = [self._m(text)]
            if b.tpsl_known or st.stop_ids:
                msgs += await self._tpsl(b, quiet=True)
            return msgs
        except Exception as e:
            log.exception("resize %s", raw)
            return [self._m(f"Ошибка при изменении {raw}: {e}")]

    async def _tpsl(self, p: Pos, quiet: bool = False) -> list:
        raw = p.symbol
        info = self.ex.market(raw)
        st = self.syms.get(raw)
        if info is None or st is None or st.detached or st.expected <= 0:
            return []
        try:
            drift = await self._check_drift(raw, info)
            if drift:
                return [] if drift == "detached" else [self._m(drift)]
            await self.ex.cancel_stops(info.symbol, st.stop_ids)
            st.stop_ids = []
            if p.tpsl_known and (p.tp or p.sl):
                st.stop_ids = await self.ex.set_stops(info.symbol, st.side, st.expected, p.tp, p.sl)
        except Exception as e:
            log.exception("tpsl %s", raw)
            return [self._m(f"Ошибка при выставлении TP/SL {raw}: {e}")]
        if quiet:
            return []
        return [self._m(f"Раст: TP/SL {raw} → TP {fmt(p.tp)} / SL {fmt(p.sl)}\n→ у тебя так же на {fmt(st.expected)}")]
