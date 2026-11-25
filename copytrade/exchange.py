"""Адаптеры биржи: BingX через ccxt (demo/live) и бумажный (режим уведомлений, тесты)."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, Optional

log = logging.getLogger("copier.exchange")


@dataclass(frozen=True)
class MarketInfo:
    symbol: str          # ccxt: "BTC/USDT:USDT"
    step: float          # шаг объёма в монетах
    min_amount: float
    max_leverage: int = 125


def ccxt_symbol(raw: str) -> str:
    """«BTCUSDT» → «BTC/USDT:USDT»."""
    return f"{raw[:-4]}/USDT:USDT"


def _market_info(m: dict) -> MarketInfo:
    step = m.get("precision", {}).get("amount") or 0.0001
    lim = m.get("limits", {})
    min_amt = (lim.get("amount") or {}).get("min") or step
    max_lev = (lim.get("leverage") or {}).get("max") or 125
    return MarketInfo(m["symbol"], float(step), float(min_amt), int(max_lev))


class PaperAdapter:
    """Позиции в памяти. Цены — через price_fn (в проде публичный тикер BingX)."""

    name = "paper"

    def __init__(self, markets: Optional[dict] = None, price_fn: Optional[Callable] = None, equity: float = 200.0,
                 hedged: bool = False):
        self.markets = markets or {}
        self._price_fn = price_fn
        self.equity = equity
        self.hedged = hedged
        self.positions: dict = {}      # one-way: ccxt symbol -> (side, amount)
        self.hpos: dict = {}           # hedge: (ccxt symbol, side) -> amount
        self.stops: dict = {}          # ccxt symbol -> {"tp":..,"sl":..,"amount":..}
        self.orders: list = []
        self._public = None

    async def load(self) -> None:
        if self.markets:
            return
        import ccxt.async_support as ccxt
        self._public = ccxt.bingx({"options": {"defaultType": "swap"}})
        mk = await self._public.load_markets()
        self.markets = {s: _market_info(m) for s, m in mk.items() if m.get("swap") and m.get("linear")}

    async def close(self) -> None:
        if self._public:
            await self._public.close()

    def market(self, raw: str) -> Optional[MarketInfo]:
        return self.markets.get(ccxt_symbol(raw))

    async def price(self, sym: str) -> float:
        if self._price_fn:
            return self._price_fn(sym)
        t = await self._public.fetch_ticker(sym)
        return float(t["last"])

    async def position(self, sym: str, side: Optional[str] = None):
        if self.hedged:
            sides = [side] if side else ["long", "short"]
            for s in sides:
                amt = self.hpos.get((sym, s), 0.0)
                if amt > 1e-12:
                    return s, amt
            return None, 0.0
        return self.positions.get(sym, (None, 0.0))

    async def open_positions(self, sym: str) -> list:
        """Все открытые позиции по монете: [{side, amount, entry, pnl}] (у бумажного входа и PnL нет)."""
        if self.hedged:
            return [{"side": s, "amount": a, "entry": None, "pnl": None}
                    for (ps, s), a in self.hpos.items() if ps == sym and a > 1e-12]
        side, amt = self.positions.get(sym, (None, 0.0))
        return [{"side": side, "amount": amt, "entry": None, "pnl": None}] if amt > 1e-12 else []

    async def balance(self):
        used = 0.0
        return self.equity, self.equity - used

    async def prepare(self, sym: str, leverage: int) -> None:
        self.orders.append(("prepare", sym, leverage))

    async def market_order(self, sym: str, side: str, amount: float, reduce_only: bool,
                           pos_side: Optional[str] = None) -> float:
        px = await self.price(sym)
        if self.hedged:
            ps = pos_side or ("long" if side == "buy" else "short")
            if reduce_only:
                ps = pos_side or ("short" if side == "buy" else "long")
                left = round(self.hpos.get((sym, ps), 0.0) - amount, 10)
                if left > 1e-12:
                    self.hpos[(sym, ps)] = left
                else:
                    self.hpos.pop((sym, ps), None)
            else:
                self.hpos[(sym, ps)] = round(self.hpos.get((sym, ps), 0.0) + amount, 10)
            self.orders.append(("market", sym, side, amount, reduce_only))
            return px
        cur_side, cur = self.positions.get(sym, (None, 0.0))
        pos_side = "long" if side == "buy" else "short"
        if reduce_only:
            left = round(cur - amount, 10)
            self.positions[sym] = (cur_side, left) if left > 1e-12 else (None, 0.0)
        else:
            self.positions[sym] = (pos_side, round(cur + amount, 10))
        self.orders.append(("market", sym, side, amount, reduce_only))
        return px

    async def set_stops(self, sym: str, pos_side: str, amount: float, tp, sl) -> list:
        self.stops[sym] = {"tp": tp, "sl": sl, "amount": amount}
        self.orders.append(("stops", sym, tp, sl, amount))
        return [f"{sym}:tp", f"{sym}:sl"]

    async def cancel_stops(self, sym: str, ids: list) -> None:
        self.stops.pop(sym, None)


class CcxtAdapter:
    """BingX USDT-M perpetual. sandbox=True — демо-счёт VST."""

    def __init__(self, api_key: str, secret: str, sandbox: bool):
        import ccxt.async_support as ccxt
        self.name = "demo" if sandbox else "live"
        self.currency = "VST" if sandbox else "USDT"      # демо-счёт BingX считается в виртуальных VST
        self.ex = ccxt.bingx({"apiKey": api_key, "secret": secret, "enableRateLimit": True,
                              "options": {"defaultType": "swap"}})
        if sandbox:
            self.ex.set_sandbox_mode(True)
        self.markets: dict = {}
        self.hedged = False

    async def load(self) -> None:
        mk = await self.ex.load_markets()
        self.markets = {s: _market_info(m) for s, m in mk.items() if m.get("swap") and m.get("linear")}
        # режим позиций (hedge/one-way) — настройка всего счёта Никиты; не меняем, только подстраиваемся
        try:
            mode = await self.ex.fetch_position_mode()
            self.hedged = bool(mode.get("hedged"))
        except Exception as e:
            log.warning("режим позиций не определён, считаю one-way: %s", e)

    async def close(self) -> None:
        await self.ex.close()

    def market(self, raw: str) -> Optional[MarketInfo]:
        return self.markets.get(ccxt_symbol(raw))

    async def price(self, sym: str) -> float:
        t = await self.ex.fetch_ticker(sym)
        return float(t["last"])

    async def position(self, sym: str, side: Optional[str] = None):
        """(сторона, объём в монетах). В hedge-режиме — позиция именно этой стороны."""
        found = []
        for p in await self.ex.fetch_positions([sym]):
            amt = float(p.get("contracts") or 0) * float(p.get("contractSize") or 1)
            if amt > 0:
                found.append((p.get("side"), amt))
        if side:
            for f in found:
                if f[0] == side:
                    return f
            if self.hedged:
                return None, 0.0
        return found[0] if found else (None, 0.0)

    async def open_positions(self, sym: str) -> list:
        out = []
        for p in await self.ex.fetch_positions([sym]):
            amt = float(p.get("contracts") or 0) * float(p.get("contractSize") or 1)
            if amt > 0:
                entry, pnl = p.get("entryPrice"), p.get("unrealizedPnl")
                out.append({"side": p.get("side"), "amount": amt, "entry": float(entry) if entry else None,
                            "pnl": float(pnl) if pnl is not None else None})
        return out

    async def balance(self):
        b = await self.ex.fetch_balance()
        cur = b.get(self.currency, {})
        return float(cur.get("total") or 0), float(cur.get("free") or 0)

    def _p(self, extra: dict) -> dict:
        return {**extra, "hedged": True} if self.hedged else extra

    async def prepare(self, sym: str, leverage: int) -> None:
        try:
            await self.ex.set_margin_mode("isolated", sym)
        except Exception as e:   # уже isolated / есть позиция
            log.info("margin mode %s: %s", sym, e)
        sides = ["LONG", "SHORT"] if self.hedged else ["BOTH"]
        for s in sides:
            await self.ex.set_leverage(leverage, sym, {"side": s})

    async def market_order(self, sym: str, side: str, amount: float, reduce_only: bool,
                           pos_side: Optional[str] = None) -> float:
        params = self._p({"reduceOnly": True} if reduce_only else {})
        o = await self.ex.create_order(sym, "market", side, amount, None, params)
        return float(o.get("average") or o.get("price") or 0) or await self.price(sym)

    async def set_stops(self, sym: str, pos_side: str, amount: float, tp, sl) -> list:
        close_side = "sell" if pos_side == "long" else "buy"
        ids = []
        for key, px in (("takeProfitPrice", tp), ("stopLossPrice", sl)):
            if px:
                o = await self.ex.create_order(sym, "market", close_side, amount, None,
                                               self._p({key: px, "reduceOnly": True}))
                ids.append(o["id"])
        return ids

    async def cancel_stops(self, sym: str, ids: list) -> None:
        for oid in ids:
            try:
                await self.ex.cancel_order(oid, sym)
            except Exception as e:
                log.info("cancel %s: %s", oid, e)
