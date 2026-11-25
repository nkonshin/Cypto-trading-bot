"""Логика копирования: событие Раста → ордера на нашей бирже.

- Бот управляет только позициями, которые открыл сам (self.syms). Ручные позиции Никиты не трогает никогда
  и не открывает копию поверх своей позиции в ту же сторону.
- Режим подтверждений (auto=False, по умолчанию): на каждое действие Раста — сообщение «Делаем / Не делаем».
  «Делаем» приводит копию к его ТЕКУЩЕЙ позиции (а не к той, что была в момент сообщения).
- Автомат (auto=True): монеты из auto_symbols и уже подтверждённые копии ведутся сразу; новые монеты — кнопкой.
- Объём: его объём × k, где k считает App в момент сделки (наш баланс / его баланс × множитель).
  Добавка: его добавка × текущий k. Сокращение: наш объём уменьшается в той же доле, что у него.
- Если наша позиция разошлась с ожидаемой (сработал наш стоп, ручное вмешательство), монета «отцеплена»:
  бот её больше не трогает, пока Раст её не закроет; тогда снимает с учёта, ничего не закрывая.
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
OFFER_TTL = 60 * 60


@dataclass
class SymState:
    side: Optional[str] = None
    expected: float = 0.0          # наш объём
    detached: bool = False
    stop_ids: list = field(default_factory=list)
    his_size: float = 0.0          # его объём на момент последней синхронизации
    tp: Optional[float] = None     # выставленные у нас стопы
    sl: Optional[float] = None


@dataclass
class Offer:
    id: str
    kind: str            # "sync" — привести копию к его позиции; "close" — закрыть копию (нет → оставить себе)
    symbol: str
    created: float


def fmt(v: Optional[float]) -> str:
    if v is None:
        return "—"
    return f"{v:,.4f}".rstrip("0").rstrip(".").replace(",", " ")


def usd(amount: Optional[float], price: Optional[float]) -> str:
    """Номинал в долларах: «~$23 587»."""
    if not amount or not price:
        return ""
    return f"~${amount * price:,.0f}".replace(",", " ")


def qty(raw: str, amount: float, price: Optional[float]) -> str:
    """«0.4 BTC (~$23 587)»."""
    d = usd(amount, price)
    return f"{fmt(amount)} {raw[:-4]}" + (f" ({d})" if d else "")


def desc(p: Pos) -> str:
    lev = f" ({p.leverage}x)" if p.leverage else ""
    stops = f", TP {fmt(p.tp)} / SL {fmt(p.sl)}" if p.tpsl_known and (p.tp or p.sl) else ""
    return f"{p.side.upper()} {p.symbol} {qty(p.symbol, p.size, p.avg)} по {fmt(p.avg)}{lev}{stops}"


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

    def managed(self, raw: str) -> bool:
        st = self.syms.get(raw)
        return st is not None and not st.detached

    # ---------- сохранение ----------
    def dump(self) -> dict:
        """Наши копии — для сохранения на диск: после перезапуска бот должен помнить, что открывал он."""
        return {raw: {"side": st.side, "expected": st.expected, "detached": st.detached,
                      "stop_ids": list(st.stop_ids), "his_size": st.his_size, "tp": st.tp, "sl": st.sl}
                for raw, st in self.syms.items()}

    def restore(self, data: dict) -> None:
        self.syms = {raw: SymState(v.get("side"), float(v.get("expected", 0)), bool(v.get("detached")),
                                   list(v.get("stop_ids") or []), float(v.get("his_size") or 0),
                                   v.get("tp"), v.get("sl"))
                     for raw, v in (data or {}).items()}

    # ---------- предложения ----------
    def _offer(self, kind: str, raw: str, text: str, yes="Делаем", no="Не делаем") -> Msg:
        self._drop_offers(raw)                 # по монете живо только последнее предложение
        o = Offer(uuid.uuid4().hex[:10], kind, raw, time.time())
        self.offers[o.id] = o
        return self._m(text + "\n(кнопки действуют 60 мин; «Делаем» подгонит копию под его позицию на момент нажатия)",
                       (("✅ " + yes, f"copy:offer:{o.id}:yes"), ("✖️ " + no, f"copy:offer:{o.id}:no")))

    def _drop_offers(self, raw: str) -> None:
        for oid in [o.id for o in self.offers.values() if o.symbol == raw]:
            self.offers.pop(oid, None)

    async def answer_offer(self, oid: str, yes: bool, rast: dict, k: Optional[float]) -> list:
        """Ответ на кнопку. rast — текущие подтверждённые позиции Раста (symbol -> Pos)."""
        o = self.offers.pop(oid, None)
        if o is None or time.time() - o.created > OFFER_TTL:
            return [self._m("Это предложение уже неактуально (есть более новое или прошло 60 мин).")]
        if not yes:
            if o.kind == "close":
                self.syms.pop(o.symbol, None)
                return [self._m(f"Ок, {o.symbol} оставляю тебе, больше её не трогаю.")]
            return [self._m(f"Ок, по {o.symbol} ничего не делаю.")]
        out = await self._sync(o.symbol, rast.get(o.symbol), k)
        return [self._m(("❌ " if "ОШИБКА" in m.text else "✅ Готово: ") + m.text[len(self._tag()):], m.buttons)
                for m in out]

    def _step(self, raw: str) -> float:
        info = self.ex.market(raw)
        return info.step if info else 0.0001

    def _estimate(self, p: Pos, k: Optional[float]) -> str:
        if not k:
            return "объём посчитаю в момент нажатия"
        amt = floor_step(p.size * k, self._step(p.symbol))
        px = p.mark or p.avg
        lev = min(p.leverage or self.default_leverage, self.max_leverage)
        margin = (", маржа ~$" + f"{amt * px / lev:,.0f}".replace(",", " ") + f" при {lev}x") if px else ""
        return f"у тебя будет {p.side.upper()} {qty(p.symbol, amt, px)}{margin} (k={k:.3f})"

    # ---------- события ----------
    async def on_event(self, ev: Event, k: Optional[float], auto: bool = True) -> list:
        s, a, b = ev.symbol, ev.before, ev.after
        st = self.syms.get(s)
        if ev.kind == "close" and (st is None or st.detached):
            self._drop_offers(s)
            if st is not None:
                self.syms.pop(s, None)
                return [self._m(f"Раст закрыл {s}. Твою позицию по {s} не трогаю (она была отцеплена).")]
            return [self._m(f"Раст закрыл {desc(a)} — её не копировали.")]
        if ev.kind in ("increase", "decrease", "tpsl") and not self.managed(s):
            what = (f"TP {fmt(b.tp)} / SL {fmt(b.sl)}" if ev.kind == "tpsl"
                    else f"{'добавил' if ev.kind == 'increase' else 'сократил'} {fmt(a.size)} → {fmt(b.size)}")
            return [self._m(f"Раст: {s} {what} (эту позицию не копирую)")]
        if auto and (s in self.auto_symbols or self.managed(s)):
            self._drop_offers(s)
            if ev.kind == "tpsl":
                return await self._tpsl(b)
            return await self._sync(s, b, k)
        # подтверждение
        if ev.kind == "open":
            note = "" if s in self.auto_symbols else "\nЭто не BTC — только по подтверждению."
            return [self._offer("sync", s, f"Раст открыл {desc(b)}.{note}\nКопируем? {self._estimate(b, k)}.")]
        if ev.kind == "flip":
            head = f"Раст перевернул {s} в {desc(b)}."
            if self.managed(s):
                return [self._offer("sync", s, f"{head}\nЗакрыть твою копию и открыть: {self._estimate(b, k)}?")]
            return [self._offer("sync", s, f"{head}\nКопируем? {self._estimate(b, k)}.")]
        if ev.kind == "close":
            return [self._offer("close", s, f"Раст закрыл {s}. Закрыть твою копию "
                                            f"({st.side.upper()} {qty(s, st.expected, a.mark or a.avg)}) по рынку?",
                                no="Оставить себе")]
        if ev.kind in ("increase", "decrease"):
            verb = "добавил" if ev.kind == "increase" else "сократил"
            if ev.kind == "increase":
                after = st.expected + floor_step((b.size - st.his_size) * (k or 0), self._step(s))
            else:
                after = floor_step(st.expected * b.size / st.his_size, self._step(s)) if st.his_size else st.expected
            px = b.mark or b.avg
            return [self._offer("sync", s, f"Раст {verb} {s}: {qty(s, a.size, px)} → {qty(s, b.size, px)}.\n"
                                           f"Делаем? Твоя копия станет ~{qty(s, after, px)} "
                                           f"(сейчас {qty(s, st.expected, px)}).")]
        if ev.kind == "tpsl":
            return [self._offer("sync", s, f"Раст переставил {s}: TP {fmt(b.tp)} / SL {fmt(b.sl)}.\n"
                                           f"Переставить у тебя так же?")]
        return []

    async def resync(self, rast: dict, k: Optional[float], reason: str) -> list:
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
                msgs.append(self._offer("sync", raw, f"У Раста открыт {desc(p)}, у тебя копии нет.\n"
                                                     f"Открыть по рынку? {self._estimate(p, k)}.",
                                        yes="Открыть", no="Не открывать"))
        for raw in list(self.syms):
            if self.managed(raw) and raw not in rast:
                st = self.syms[raw]
                msgs.append(self._offer("close", raw, f"Раст уже закрыл {raw}, а у тебя копия открыта "
                                                      f"({st.side.upper()} {fmt(st.expected)}). Закрыть?",
                                        yes="Закрыть", no="Оставить себе"))
        if not rast and not self.syms:
            lines.append("Позиций нет ни у Раста, ни у тебя — жду следующую сделку.")
        head = self._m(f"{reason}\n" + "\n".join(lines)) if lines else self._m(reason)
        return [head] + msgs

    async def close_all(self) -> list:
        msgs = []
        for raw in list(self.syms):
            if self.managed(raw):
                msgs += await self._close(raw, manual=True)
        return msgs

    # ---------- синхронизация с его текущей позицией ----------
    async def _sync(self, raw: str, his: Optional[Pos], k: Optional[float]) -> list:
        st = self.syms.get(raw)
        if st is not None and st.detached:
            if his is None:
                self.syms.pop(raw, None)
            return [self._m(f"{raw} отцеплена (ты менял её руками или сработал стоп) — не трогаю.")]
        if his is None:
            if st is None:
                return [self._m(f"У Раста уже нет позиции по {raw}, а у тебя копии нет — делать нечего.")]
            return await self._close(raw)
        if st is None:
            return await self._open(his, k)
        info = self.ex.market(raw)
        drift = await self._check_drift(raw, info)
        if drift:
            return [] if drift == "detached" else [self._m(drift)]
        if st.side != his.side:
            return await self._close(raw) + await self._open(his, k)
        msgs = []
        if his.size > st.his_size * 1.0001:
            msgs += await self._add(raw, his, k)
        elif his.size < st.his_size * 0.9999:
            msgs += await self._reduce(raw, his)
        if his.tpsl_known and (his.tp, his.sl) != (st.tp, st.sl):
            msgs += await self._tpsl(his, quiet=bool(msgs))
        if not msgs:
            msgs = [self._m(f"{raw}: копия уже совпадает с позицией Раста ({fmt(st.expected)}).")]
        return msgs

    async def _open(self, p: Pos, k: Optional[float]) -> list:
        info = self.ex.market(p.symbol)
        his = f"Раст: {desc(p)}"
        if info is None:
            return [self._m(f"{his}\n→ монеты нет на BingX, пропускаю.")]
        if not k or k <= 0:
            return [self._m(f"{his}\n→ не знаю его баланс, не могу посчитать пропорцию. Пропускаю.")]
        lev = min(p.leverage or self.default_leverage, info.max_leverage, self.max_leverage)
        amount = floor_step(p.size * k, info.step)
        if amount < info.min_amount:
            return [self._m(f"{his}\n→ объём {fmt(p.size * k)} (k={k:.3f}) меньше минимального лота "
                            f"{fmt(info.min_amount)}, пропускаю.")]
        try:
            # своя ручная позиция в ту же сторону (в one-way — в любую) слилась бы с копией: не смешиваем
            own_side, own = await self.ex.position(info.symbol, p.side)
            if own > 0:
                return [self._m(f"{his}\n→ у тебя уже есть своя позиция {(own_side or '').upper()} {p.symbol} "
                                f"{fmt(own)}. Копию не открываю, чтобы не смешать с твоей.")]
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
        self.syms[p.symbol] = SymState(p.side, amount, his_size=p.size)
        note = " (ужал под маржу)" if cut else ""
        margin = f"{amount * fill / lev:,.0f}".replace(",", " ")
        msgs = [self._m(f"{his}\n→ открыл {p.side.upper()} {qty(p.symbol, amount, fill)} по ~{fmt(fill)} "
                        f"(k={k:.3f}, {lev}x), маржа ~${margin}{note}")]
        if p.tpsl_known and (p.tp or p.sl):
            msgs += await self._tpsl(p)
        return msgs

    async def _close(self, raw: str, manual: bool = False) -> list:
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
        return [self._m(f"{head}\n→ закрыл {(side or '').upper()} {qty(raw, amt, fill)} по ~{fmt(fill)}")]

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

    async def _add(self, raw: str, his: Pos, k: Optional[float]) -> list:
        st, info = self.syms[raw], self.ex.market(raw)
        head = f"Раст добавил {raw}: {fmt(st.his_size)} → {fmt(his.size)}"
        if not k or k <= 0:
            return [self._m(f"{head}\n→ не знаю его баланс, добавку пропускаю.")]
        want = (his.size - st.his_size) * k
        add = floor_step(want, info.step)
        if add < info.min_amount:
            st.his_size = his.size
            return [self._m(f"{head}\n→ добавка {fmt(want)} меньше лота, пропускаю.")]
        try:
            px = await self.ex.price(info.symbol)
            _, free = await self.ex.balance()
            lev = min(his.leverage or self.default_leverage, info.max_leverage, self.max_leverage)
            add, cut = fit_margin(add, px, lev, free, info.step, self.margin_frac)
            if add < info.min_amount:
                return [self._m(f"{head}\n→ не хватает маржи на добавку, пропускаю.")]
            fill = await self.ex.market_order(info.symbol, "buy" if st.side == "long" else "sell", add, False, st.side)
        except Exception as e:
            log.exception("add %s", raw)
            return [self._m(f"{head}\n→ ОШИБКА добавки: {e}")]
        st.expected = round(st.expected + add, 10)
        st.his_size = his.size
        msgs = [self._m(f"{head}\n→ добавил {qty(raw, add, fill)} по ~{fmt(fill)}, "
                        f"теперь {qty(raw, st.expected, fill)}")]
        if st.stop_ids:
            msgs += await self._tpsl(his, quiet=True, force=True)
        return msgs

    async def _reduce(self, raw: str, his: Pos) -> list:
        st, info = self.syms[raw], self.ex.market(raw)
        head = f"Раст сократил {raw}: {fmt(st.his_size)} → {fmt(his.size)}"
        target = floor_step(st.expected * his.size / st.his_size, info.step) if st.his_size else st.expected
        cut_amt = floor_step(st.expected - target, info.step)
        if cut_amt < info.min_amount:
            return [self._m(f"{head}\n→ сокращение {fmt(st.expected - target)} меньше лота, оставляю.")]
        try:
            fill = await self.ex.market_order(info.symbol, "sell" if st.side == "long" else "buy", cut_amt, True,
                                              st.side)
        except Exception as e:
            log.exception("reduce %s", raw)
            return [self._m(f"{head}\n→ ОШИБКА сокращения: {e}")]
        st.expected = round(st.expected - cut_amt, 10)
        st.his_size = his.size
        msgs = [self._m(f"{head}\n→ сократил на {qty(raw, cut_amt, fill)} по ~{fmt(fill)}, "
                        f"осталось {qty(raw, st.expected, fill)}")]
        if st.stop_ids:
            msgs += await self._tpsl(his, quiet=True, force=True)
        return msgs

    async def _tpsl(self, p: Pos, quiet: bool = False, force: bool = False) -> list:
        raw = p.symbol
        info = self.ex.market(raw)
        st = self.syms.get(raw)
        if info is None or st is None or st.detached or st.expected <= 0:
            return []
        tp, sl = (p.tp, p.sl) if p.tpsl_known else (st.tp, st.sl)
        if not force and (tp, sl) == (st.tp, st.sl) and st.stop_ids:
            return []
        try:
            drift = await self._check_drift(raw, info)
            if drift:
                return [] if drift == "detached" else [self._m(drift)]
            await self.ex.cancel_stops(info.symbol, st.stop_ids)
            st.stop_ids = []
            if tp or sl:
                st.stop_ids = await self.ex.set_stops(info.symbol, st.side, st.expected, tp, sl)
            st.tp, st.sl = tp, sl
        except Exception as e:
            log.exception("tpsl %s", raw)
            return [self._m(f"ОШИБКА при выставлении TP/SL {raw}: {e}")]
        if quiet:
            return []
        return [self._m(f"TP/SL {raw} → TP {fmt(tp)} / SL {fmt(sl)} на весь объём {fmt(st.expected)}")]
