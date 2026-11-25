"""Логика копирования: событие Раста → ордера на нашей бирже.

- Позиция = пара (монета, сторона). У Раста и у Никиты hedge-режим: LONG и SHORT по BTC — две независимые
  позиции, и копии у них независимые.
- Бот управляет только позициями, которые открыл сам (self.syms). Ручные позиции Никиты не трогает никогда
  и не открывает копию поверх своей позиции в ту же сторону.
- Режим подтверждений (auto=False, по умолчанию): на каждое действие Раста — сообщение «Делаем / Не делаем».
  «Делаем» приводит копию к его ТЕКУЩЕЙ позиции (а не к той, что была в момент сообщения).
- Автомат (auto=True): монеты из auto_symbols и уже подтверждённые копии ведутся сразу; новые монеты — кнопкой.
- Позиция Раста без копии (отказался или пропустил): каждое его действие по ней приходит с кнопкой «Войти»,
  и войти можно из /copy в любой момент. Посреди позиции бот сам не входит даже в автомате.
- В сообщениях о его открытой позиции — его текущий PnL по живой цене BingX (как в его панели: $ и % от маржи).
- Объём: его объём × k, где k считает App в момент сделки (наш баланс / его баланс × множитель).
  Добавка: его добавка × текущий k. Сокращение: наш объём уменьшается в той же доле, что у него.
- Если наша позиция разошлась с ожидаемой (сработал наш стоп, ручное вмешательство), она «отцеплена»:
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
    key: tuple           # (symbol, side)
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


def his_pnl(p: Pos, px: Optional[float]):
    """Нереализованный PnL Раста по цене px: ($, %) — как в его панели OKX, без комиссий.
    OKX считает % от начальной маржи (номинал / плечо), а не от поля Margin: Раст доливает маржу в позицию
    (25.11: Margin 662.70 при начальной 463.23 → панель показывала 18.25%, а не 12.76%)."""
    if not (px and p.avg and p.size):
        return None
    v = (px - p.avg) * p.size * (1 if p.side == "long" else -1)
    base = p.size * p.avg / p.leverage if p.leverage else p.margin
    return v, (v / base * 100 if base else None)


def pnl_str(v: float, pct: Optional[float] = None) -> str:
    """«+$172 (+46.6%)», «-$5.00»."""
    a = abs(v)
    num = f"{a:,.0f}".replace(",", " ") if a >= 10 else f"{a:.2f}"
    out = f"{'+' if v >= 0 else '-'}${num}"
    if pct is not None:
        out += f" ({'+' if pct >= 0 else '-'}{abs(pct):.1f}%)"
    return out


def desc(p: Pos) -> str:
    lev = f" ({p.leverage}x)" if p.leverage else ""
    stops = f", TP {fmt(p.tp)} / SL {fmt(p.sl)}" if p.tpsl_known and (p.tp or p.sl) else ""
    return f"{p.side.upper()} {p.symbol} {qty(p.symbol, p.size, p.avg)} по {fmt(p.avg)}{lev}{stops}"


def kname(key: tuple) -> str:
    return f"{key[1].upper()} {key[0]}"


def key_str(key: tuple) -> str:
    return f"{key[0]}:{key[1]}"


class Copier:
    def __init__(self, adapter, default_leverage: int = 10, margin_frac: float = 0.95, max_leverage: int = 125,
                 auto_symbols=("BTCUSDT",)):
        self.ex = adapter
        self.default_leverage = default_leverage
        self.max_leverage = max_leverage
        self.margin_frac = margin_frac
        self.auto_symbols = set(auto_symbols)
        self.syms: dict = {}           # (symbol, side) -> SymState (только наши копии)
        self.offers: dict = {}         # id -> Offer

    def _tag(self) -> str:
        return {"paper": "[тест] ", "demo": "[демо] "}.get(self.ex.name, "")

    def _m(self, text: str, buttons=()) -> Msg:
        return Msg(self._tag() + text, tuple(buttons))

    def managed(self, key: tuple) -> bool:
        st = self.syms.get(key)
        return st is not None and not st.detached

    # ---------- сохранение ----------
    def dump(self) -> dict:
        """Наши копии — для сохранения на диск: после перезапуска бот должен помнить, что открывал он."""
        return {key_str(k): {"side": st.side, "expected": st.expected, "detached": st.detached,
                             "stop_ids": list(st.stop_ids), "his_size": st.his_size, "tp": st.tp, "sl": st.sl}
                for k, st in self.syms.items()}

    def restore(self, data: dict) -> None:
        self.syms = {}
        for ks, v in (data or {}).items():
            raw, _, side = ks.partition(":")
            side = side or v.get("side")
            self.syms[(raw, side)] = SymState(side, float(v.get("expected", 0)), bool(v.get("detached")),
                                              list(v.get("stop_ids") or []), float(v.get("his_size") or 0),
                                              v.get("tp"), v.get("sl"))

    # ---------- предложения ----------
    def _offer(self, kind: str, key: tuple, text: str, yes="Делаем", no="Не делаем") -> Msg:
        self._drop_offers(key)                 # по позиции живо только последнее предложение
        o = Offer(uuid.uuid4().hex[:10], kind, key, time.time())
        self.offers[o.id] = o
        return self._m(text + f"\n(кнопки действуют 60 мин; «{yes}» — по его позиции на момент нажатия)",
                       (("✅ " + yes, f"copy:offer:{o.id}:yes"), ("✖️ " + no, f"copy:offer:{o.id}:no")))

    def _drop_offers(self, key: tuple) -> None:
        for oid in [o.id for o in self.offers.values() if o.key == key]:
            self.offers.pop(oid, None)

    async def answer_offer(self, oid: str, yes: bool, rast: dict, k: Optional[float]) -> list:
        """Ответ на кнопку. rast — текущие подтверждённые позиции Раста ((symbol, side) -> Pos)."""
        o = self.offers.pop(oid, None)
        if o is None or time.time() - o.created > OFFER_TTL:
            return [self._m("Это предложение уже неактуально (есть более новое или прошло 60 мин).")]
        if not yes:
            if o.kind == "close":
                self.syms.pop(o.key, None)
                return [self._m(f"Ок, {kname(o.key)} оставляю тебе, больше её не трогаю.")]
            return [self._m(f"Ок, по {kname(o.key)} ничего не делаю. Если Раст что-то поменяет в ней — "
                            f"снова предложу войти; войти раньше — /copy.")]
        out = await self._sync(o.key, rast.get(o.key), k)
        return [self._m(("❌ " if "ОШИБКА" in m.text else "✅ Готово: ") + m.text[len(self._tag()):], m.buttons)
                for m in out]

    def _step(self, raw: str) -> float:
        info = self.ex.market(raw)
        return info.step if info else 0.0001

    async def _live_px(self, p: Pos) -> Optional[float]:
        """Текущая цена с BingX; не получилось — mark с его панели."""
        info = self.ex.market(p.symbol)
        if info is not None:
            try:
                return float(await self.ex.price(info.symbol))
            except Exception as e:
                log.warning("price %s: %r", p.symbol, e)
        return p.mark

    @staticmethod
    def _pnl_line(p: Pos, px: Optional[float]) -> str:
        r = his_pnl(p, px)
        return f"PnL Раста сейчас: {pnl_str(*r)} при цене {fmt(px)}" if r else ""

    async def _pnl_text(self, p: Pos) -> str:
        """«PnL Раста сейчас: …» отдельной строкой (или пусто, если цены нет)."""
        line = self._pnl_line(p, await self._live_px(p))
        return f"\n{line}" if line else ""

    async def _result_text(self, p: Pos) -> str:
        """Итог закрытой позиции по текущей цене (≈ цене закрытия: событие приходит через секунды)."""
        r = his_pnl(p, await self._live_px(p))
        return f" Результат у него ~{pnl_str(*r)}." if r else ""

    def _entry_offer(self, key: tuple, his: Pos, k: Optional[float], head: str, px: Optional[float],
                     yes="Войти", no="Не входить") -> Msg:
        pnl = self._pnl_line(his, px)
        return self._offer("sync", key, f"{head}\nСейчас у него: {desc(his)}" + (f"\n{pnl}" if pnl else "")
                           + f"\nВойти по рынку? {self._estimate(his, k, px)}.", yes=yes, no=no)

    async def offer_entry(self, key: tuple, his: Optional[Pos], k: Optional[float]) -> list:
        """Кнопка «Войти» из /copy: предложение войти в его текущую позицию."""
        if his is None:
            return [self._m(f"У Раста сейчас нет {kname(key)} — входить не во что.")]
        st = self.syms.get(key)
        if st is not None:
            tail = " — она отцеплена, не трогаю" if st.detached else ""
            return [self._m(f"{kname(key)}: копия уже есть ({fmt(st.expected)}){tail}.")]
        return [self._entry_offer(key, his, k, f"{kname(key)} — у тебя копии нет.", await self._live_px(his))]

    def _estimate(self, p: Pos, k: Optional[float], px: Optional[float] = None) -> str:
        if not k:
            return "объём посчитаю в момент нажатия"
        amt = floor_step(p.size * k, self._step(p.symbol))
        px = px or p.mark or p.avg
        lev = min(p.leverage or self.default_leverage, self.max_leverage)
        margin = (", маржа ~$" + f"{amt * px / lev:,.0f}".replace(",", " ") + f" при {lev}x") if px else ""
        return f"у тебя будет {p.side.upper()} {qty(p.symbol, amt, px)}{margin} (k={k:.3f})"

    # ---------- события ----------
    async def on_event(self, ev: Event, k: Optional[float], auto: bool = True) -> list:
        key = ev.key
        s, a, b = ev.symbol, ev.before, ev.after
        st = self.syms.get(key)
        if ev.kind == "close" and (st is None or st.detached):
            self._drop_offers(key)
            res = await self._result_text(a)
            if st is not None:
                self.syms.pop(key, None)
                return [self._m(f"Раст закрыл {kname(key)}.{res} Твою позицию не трогаю (она была отцеплена).")]
            return [self._m(f"Раст закрыл {desc(a)} — её не копировали.{res}")]
        if ev.kind in ("increase", "decrease", "tpsl") and not self.managed(key):
            px = await self._live_px(b)
            what = (f"переставил {kname(key)}: TP {fmt(b.tp)} / SL {fmt(b.sl)}" if ev.kind == "tpsl"
                    else f"{'добавил' if ev.kind == 'increase' else 'сократил'} {kname(key)}: "
                         f"{qty(s, a.size, px)} → {qty(s, b.size, px)}")
            if st is not None:                         # отцеплена: Никита ведёт её сам
                pnl = self._pnl_line(b, px)
                return [self._m(f"Раст {what} (твоя позиция отцеплена, не трогаю)" + (f"\n{pnl}" if pnl else ""))]
            return [self._entry_offer(key, b, k, f"Раст {what}. У тебя копии нет.", px)]
        if auto and (s in self.auto_symbols or self.managed(key)):
            self._drop_offers(key)
            if ev.kind == "tpsl":
                return await self._tpsl(b)
            return await self._sync(key, b, k)
        # подтверждение
        if ev.kind == "open":
            note = "" if s in self.auto_symbols else "\nЭто не BTC — только по подтверждению."
            return [self._offer("sync", key, f"Раст открыл {desc(b)}.{note}\nКопируем? {self._estimate(b, k)}.")]
        if ev.kind == "close":
            return [self._offer("close", key, f"Раст закрыл {kname(key)}.{await self._result_text(a)} "
                                              f"Закрыть твою копию "
                                              f"({qty(s, st.expected, a.mark or a.avg)}) по рынку?",
                                no="Оставить себе")]
        if ev.kind in ("increase", "decrease"):
            verb = "добавил" if ev.kind == "increase" else "сократил"
            if ev.kind == "increase":
                after = st.expected + floor_step((b.size - st.his_size) * (k or 0), self._step(s))
            else:
                after = floor_step(st.expected * b.size / st.his_size, self._step(s)) if st.his_size else st.expected
            px = await self._live_px(b)
            pnl = self._pnl_line(b, px)
            return [self._offer("sync", key, f"Раст {verb} {kname(key)}: {qty(s, a.size, px)} → {qty(s, b.size, px)}."
                                             + (f"\n{pnl}" if pnl else "") + "\n"
                                             f"Делаем? Твоя копия станет ~{qty(s, after, px)} "
                                             f"(сейчас {qty(s, st.expected, px)}).")]
        if ev.kind == "tpsl":
            return [self._offer("sync", key, f"Раст переставил {kname(key)}: TP {fmt(b.tp)} / SL {fmt(b.sl)}."
                                             f"{await self._pnl_text(b)}\nПереставить у тебя так же?")]
        return []

    async def resync(self, rast: dict, k: Optional[float], reason: str) -> list:
        """Сверка после паузы/старта: что у Раста, что у нас; по расхождениям — предложения с кнопками."""
        msgs, lines = [], []
        for key in list(self.syms):
            st = self.syms[key]
            info = self.ex.market(key[0])
            if st.detached or info is None:
                continue
            side, amt = await self.ex.position(info.symbol, st.side)
            if side != st.side or abs(amt - st.expected) > info.step * 1.5:
                self.syms.pop(key, None)
                lines.append(f"{kname(key)}: у тебя {fmt(amt)} вместо {fmt(st.expected)} — считаю твоей, не трогаю.")
        for key, p in rast.items():
            if self.managed(key):
                lines.append(f"{kname(key)}: копия на месте ({fmt(self.syms[key].expected)}), продолжаю вести.")
            else:
                px = await self._live_px(p)
                pnl = self._pnl_line(p, px)
                msgs.append(self._offer("sync", key, f"У Раста открыт {desc(p)}, у тебя копии нет."
                                                     + (f"\n{pnl}" if pnl else "") + "\n"
                                                     f"Открыть по рынку? {self._estimate(p, k, px)}.",
                                        yes="Открыть", no="Не открывать"))
        for key in list(self.syms):
            if self.managed(key) and key not in rast:
                st = self.syms[key]
                msgs.append(self._offer("close", key, f"Раст уже закрыл {kname(key)}, а у тебя копия открыта "
                                                      f"({fmt(st.expected)}). Закрыть?",
                                        yes="Закрыть", no="Оставить себе"))
        if not rast and not self.syms:
            lines.append("Позиций нет ни у Раста, ни у тебя — жду следующую сделку.")
        head = self._m(f"{reason}\n" + "\n".join(lines)) if lines else self._m(reason)
        return [head] + msgs

    async def close_all(self) -> list:
        msgs = []
        for key in list(self.syms):
            if self.managed(key):
                msgs += await self._close(key, manual=True)
        return msgs

    # ---------- синхронизация с его текущей позицией ----------
    async def _sync(self, key: tuple, his: Optional[Pos], k: Optional[float]) -> list:
        st = self.syms.get(key)
        if st is not None and st.detached:
            if his is None:
                self.syms.pop(key, None)
            return [self._m(f"{kname(key)} отцеплена (ты менял её руками или сработал стоп) — не трогаю.")]
        if his is None:
            if st is None:
                return [self._m(f"У Раста уже нет {kname(key)}, а у тебя копии нет — делать нечего.")]
            return await self._close(key)
        if st is None:
            return await self._open(his, k)
        info = self.ex.market(key[0])
        drift = await self._check_drift(key, info)
        if drift:
            return [] if drift == "detached" else [self._m(drift)]
        msgs = []
        if his.size > st.his_size * 1.0001:
            msgs += await self._add(key, his, k)
        elif his.size < st.his_size * 0.9999:
            msgs += await self._reduce(key, his)
        if his.tpsl_known and (his.tp, his.sl) != (st.tp, st.sl):
            msgs += await self._tpsl(his, quiet=bool(msgs))
        if not msgs:
            msgs = [self._m(f"{kname(key)}: копия уже совпадает с позицией Раста ({fmt(st.expected)}).")]
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
        self.syms[(p.symbol, p.side)] = SymState(p.side, amount, his_size=p.size)
        note = " (ужал под маржу)" if cut else ""
        margin = f"{amount * fill / lev:,.0f}".replace(",", " ")
        msgs = [self._m(f"{his}\n→ открыл {p.side.upper()} {qty(p.symbol, amount, fill)} по ~{fmt(fill)} "
                        f"(k={k:.3f}, {lev}x), маржа ~${margin}{note}")]
        if p.tpsl_known and (p.tp or p.sl):
            msgs += await self._tpsl(p)
        return msgs

    async def _close(self, key: tuple, manual: bool = False) -> list:
        info = self.ex.market(key[0])
        st = self.syms.get(key)
        if info is None or st is None:
            return []
        head = f"Закрываю копию {kname(key)}" if manual else f"Раст закрыл {kname(key)}"
        try:
            await self.ex.cancel_stops(info.symbol, st.stop_ids)
            side, amt = await self.ex.position(info.symbol, st.side)
            fill = None
            if amt > 0:
                fill = await self.ex.market_order(info.symbol, "sell" if side == "long" else "buy", amt, True, side)
        except Exception as e:
            log.exception("close %s", key)
            return [self._m(f"{head}\n→ ОШИБКА закрытия: {e}")]
        self.syms.pop(key, None)
        if amt <= 0:
            return [self._m(f"{head}\n→ у тебя копии уже не было (закрылась стопом?).")]
        return [self._m(f"{head}\n→ закрыл {(side or '').upper()} {qty(key[0], amt, fill)} по ~{fmt(fill)}")]

    async def _check_drift(self, key: tuple, info) -> Optional[str]:
        st = self.syms.get(key)
        if st is None or st.detached:
            return "detached"
        side, amt = await self.ex.position(info.symbol, st.side)
        if side != st.side or abs(amt - st.expected) > info.step * 1.5:
            st.detached = True
            return (f"Твоя копия {kname(key)} разошлась с ожидаемой: на бирже {fmt(amt)} {(side or '').upper()}, "
                    f"ждал {fmt(st.expected)} (сработал стоп или ты поправил руками). "
                    f"Больше её не трогаю, пока Раст её не закроет.")
        return None

    async def _add(self, key: tuple, his: Pos, k: Optional[float]) -> list:
        raw = key[0]
        st, info = self.syms[key], self.ex.market(raw)
        head = f"Раст добавил {kname(key)}: {fmt(st.his_size)} → {fmt(his.size)}"
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
            log.exception("add %s", key)
            return [self._m(f"{head}\n→ ОШИБКА добавки: {e}")]
        st.expected = round(st.expected + add, 10)
        st.his_size = his.size
        msgs = [self._m(f"{head}\n→ добавил {qty(raw, add, fill)} по ~{fmt(fill)}, "
                        f"теперь {qty(raw, st.expected, fill)}")]
        if st.stop_ids:
            msgs += await self._tpsl(his, quiet=True, force=True)
        return msgs

    async def _reduce(self, key: tuple, his: Pos) -> list:
        raw = key[0]
        st, info = self.syms[key], self.ex.market(raw)
        head = f"Раст сократил {kname(key)}: {fmt(st.his_size)} → {fmt(his.size)}"
        target = floor_step(st.expected * his.size / st.his_size, info.step) if st.his_size else st.expected
        cut_amt = floor_step(st.expected - target, info.step)
        if cut_amt < info.min_amount:
            return [self._m(f"{head}\n→ сокращение {fmt(st.expected - target)} меньше лота, оставляю.")]
        try:
            fill = await self.ex.market_order(info.symbol, "sell" if st.side == "long" else "buy", cut_amt, True,
                                              st.side)
        except Exception as e:
            log.exception("reduce %s", key)
            return [self._m(f"{head}\n→ ОШИБКА сокращения: {e}")]
        st.expected = round(st.expected - cut_amt, 10)
        st.his_size = his.size
        msgs = [self._m(f"{head}\n→ сократил на {qty(raw, cut_amt, fill)} по ~{fmt(fill)}, "
                        f"осталось {qty(raw, st.expected, fill)}")]
        if st.stop_ids:
            msgs += await self._tpsl(his, quiet=True, force=True)
        return msgs

    async def _tpsl(self, p: Pos, quiet: bool = False, force: bool = False) -> list:
        key = (p.symbol, p.side)
        info = self.ex.market(p.symbol)
        st = self.syms.get(key)
        if info is None or st is None or st.detached or st.expected <= 0:
            return []
        tp, sl = (p.tp, p.sl) if p.tpsl_known else (st.tp, st.sl)
        if not force and (tp, sl) == (st.tp, st.sl) and st.stop_ids:
            return []
        try:
            drift = await self._check_drift(key, info)
            if drift:
                return [] if drift == "detached" else [self._m(drift)]
            await self.ex.cancel_stops(info.symbol, st.stop_ids)
            st.stop_ids = []
            if tp or sl:
                st.stop_ids = await self.ex.set_stops(info.symbol, st.side, st.expected, tp, sl)
            st.tp, st.sl = tp, sl
        except Exception as e:
            log.exception("tpsl %s", key)
            return [self._m(f"ОШИБКА при выставлении TP/SL {kname(key)}: {e}")]
        if quiet:
            return []
        return [self._m(f"TP/SL {kname(key)} → TP {fmt(tp)} / SL {fmt(sl)} на весь объём {fmt(st.expected)}")]
