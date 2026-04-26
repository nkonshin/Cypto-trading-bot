"""
Meta-стратегия: определение фазы рынка + переключение стратегий.

Walk-forward:
  Train: 2024-04 — 2025-09 (18 мес)
  Test:  2025-10 — 2026-04 (6 мес)

Фазы рынка:
  Bull:  ADX > порог, цена > EMA, EMA растёт → пробойные (Momentum/Micro)
  Range: ADX < порог, цена между EMA → отскоки (Fake Breakout, BB Bounce)
  Bear:  ADX > порог, цена < EMA, EMA падает → пробойные шорты + RSI Extreme

Тестируем: ETH, BTC, SOL на 4h
"""

import asyncio
import warnings
import logging
import sys
import numpy as np
import pandas as pd
import ta

warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.WARNING, format="%(message)s", handlers=[logging.StreamHandler(sys.stdout)])

import ccxt.async_support as ccxt_async
from backtesting.backtest import Backtester
from strategies.base import BaseStrategy, Signal, SignalType


# === DATA LOADER (с большим timeout) ===

TIMEFRAME_MS = {"4h": 14_400_000, "15m": 900_000, "1h": 3_600_000}

async def load_data(symbol, timeframe, since_str, until_str):
    """Загрузка с retry и большим timeout."""
    exchange = ccxt_async.binance({
        "enableRateLimit": True,
        "timeout": 120000,
        "options": {"defaultType": "spot"},
    })
    try:
        from datetime import datetime
        since = int(datetime.strptime(since_str, "%Y-%m-%d").timestamp() * 1000)
        until = int(datetime.strptime(until_str, "%Y-%m-%d").timestamp() * 1000)
        tf_ms = TIMEFRAME_MS.get(timeframe, 14_400_000)
        all_candles = []
        cursor = since
        while True:
            for attempt in range(3):
                try:
                    candles = await exchange.fetch_ohlcv(symbol, timeframe, since=cursor, limit=1000)
                    break
                except Exception as e:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(2)
            if not candles:
                break
            candles = [c for c in candles if c[0] <= until]
            all_candles.extend(candles)
            if len(candles) < 1000 or candles[-1][0] >= until:
                break
            cursor = candles[-1][0] + tf_ms
            await asyncio.sleep(0.5)
        seen = set()
        unique = [c for c in all_candles if c[0] not in seen and not seen.add(c[0])]
        return sorted(unique, key=lambda c: c[0])
    finally:
        await exchange.close()


# === ОПРЕДЕЛЕНИЕ ФАЗЫ РЫНКА ===

def detect_regime(df, idx, adx_threshold=20, ema_period=50):
    """Определяет фазу рынка: bull / range / bear."""
    if idx < ema_period + 10:
        return "range"
    price = df.iloc[idx]["close"]
    ema = df.iloc[idx][f"ema_{ema_period}"]
    adx = df.iloc[idx]["adx_regime"]
    ema_slope = (df.iloc[idx][f"ema_{ema_period}"] - df.iloc[idx - 5][f"ema_{ema_period}"]) / df.iloc[idx - 5][f"ema_{ema_period}"] * 100

    if adx < adx_threshold:
        return "range"
    if price > ema and ema_slope > 0:
        return "bull"
    if price < ema and ema_slope < 0:
        return "bear"
    return "range"


def add_regime_indicators(df, ema_period=50):
    """Добавляет индикаторы для определения режима."""
    df = df.copy()
    df[f"ema_{ema_period}"] = ta.trend.ema_indicator(df["close"], ema_period)
    adx = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], 14)
    df["adx_regime"] = adx.adx()
    return df


# === ПОДСТРАТЕГИИ ===

def momentum_signal(df, idx, channel_period=10, volume_mult=1.5):
    """Пробой Donchian Channel (упрощённый Momentum Breakout)."""
    if idx < channel_period + 20:
        return None
    last = df.iloc[idx]
    dc_high = df["high"].iloc[idx - channel_period:idx].max()
    dc_low = df["low"].iloc[idx - channel_period:idx].min()
    price = last["close"]
    vol_ok = last["volume"] > last["vol_sma"] * volume_mult if "vol_sma" in df else True
    macd_ok = last.get("macd_hist", 0)

    if price > dc_high and vol_ok and macd_ok > 0:
        return "buy"
    if price < dc_low and vol_ok and macd_ok < 0:
        return "sell"
    return None


def fake_breakout_signal(df, idx, channel_period=20, wick_pct=0.5):
    """Ложный пробой — свеча пробила тенью, но закрылась внутри."""
    if idx < channel_period + 5:
        return None
    last = df.iloc[idx]
    dc_high = df["high"].iloc[idx - channel_period:idx].max()
    dc_low = df["low"].iloc[idx - channel_period:idx].min()
    price, high, low = last["close"], last["high"], last["low"]
    atr = last.get("atr", 1)

    if high > dc_high and price < dc_high:
        wick = high - max(price, last["open"])
        if wick > atr * wick_pct:
            return "sell"
    if low < dc_low and price > dc_low:
        wick = min(price, last["open"]) - low
        if wick > atr * wick_pct:
            return "buy"
    return None


def bb_bounce_signal(df, idx, rsi_os=30, rsi_ob=70):
    """Отскок от Bollinger Bands + RSI."""
    if idx < 30:
        return None
    last = df.iloc[idx]
    price, rsi = last["close"], last.get("rsi", 50)
    bb_upper = last.get("bb_upper")
    bb_lower = last.get("bb_lower")
    if bb_upper is None or pd.isna(bb_upper):
        return None
    bull_candle = last["close"] > last["open"]
    bear_candle = last["close"] < last["open"]
    if price <= bb_lower and rsi < rsi_os and bull_candle:
        return "buy"
    if price >= bb_upper and rsi > rsi_ob and bear_candle:
        return "sell"
    return None


def rsi_extreme_signal(df, idx, rsi_low=30, rsi_high=70):
    """RSI экстремум + разворотная свеча."""
    if idx < 20:
        return None
    last = df.iloc[idx]
    prev = df.iloc[idx - 1]
    rsi = last.get("rsi", 50)
    bull_rev = last["close"] > last["open"] and prev["close"] < prev["open"]
    bear_rev = last["close"] < last["open"] and prev["close"] > prev["open"]
    if rsi < rsi_low and bull_rev:
        return "buy"
    if rsi > rsi_high and bear_rev:
        return "sell"
    return None


# === META-СТРАТЕГИЯ ===

class MetaStrategy(BaseStrategy):
    name = "meta"
    timeframe = "4h"
    min_candles = 210
    risk_category = "moderate"

    def __init__(self, adx_threshold=20, ema_period=50,
                 bull_strategy="momentum", range_strategy="fake_breakout",
                 bear_strategy="momentum",
                 momentum_channel=10, momentum_vol=1.5,
                 fake_channel=20, fake_wick=0.5,
                 bb_rsi_os=30, bb_rsi_ob=70,
                 rsi_low=30, rsi_high=70,
                 anomaly_filter=True, anomaly_mult=2.0):
        self.adx_threshold = adx_threshold
        self.ema_period = ema_period
        self.bull_strategy = bull_strategy
        self.range_strategy = range_strategy
        self.bear_strategy = bear_strategy
        self.momentum_channel = momentum_channel
        self.momentum_vol = momentum_vol
        self.fake_channel = fake_channel
        self.fake_wick = fake_wick
        self.bb_rsi_os = bb_rsi_os
        self.bb_rsi_ob = bb_rsi_ob
        self.rsi_low = rsi_low
        self.rsi_high = rsi_high
        self.anomaly_filter = anomaly_filter
        self.anomaly_mult = anomaly_mult
        self.description = f"Meta: ADX{adx_threshold} EMA{ema_period} bull={bull_strategy} range={range_strategy}"

    def precompute(self, df):
        df = df.copy()
        df = add_regime_indicators(df, self.ema_period)
        # Общие индикаторы
        df["atr"] = ta.volatility.average_true_range(df["high"], df["low"], df["close"], 14)
        df["atr_pct"] = df["atr"] / df["close"] * 100
        df["rsi"] = ta.momentum.rsi(df["close"], 14)
        df["vol_sma"] = df["volume"].rolling(20).mean()
        macd = ta.trend.MACD(df["close"])
        df["macd_hist"] = macd.macd_diff()
        bb = ta.volatility.BollingerBands(df["close"], 20, 2.0)
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_lower"] = bb.bollinger_lband()
        return df

    def analyze_at(self, df, idx, symbol):
        if idx < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="wait")
        last = df.iloc[idx]
        if pd.isna(last["adx_regime"]):
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="nan")

        # Anomaly filter
        if self.anomaly_filter:
            candle_size = abs(last["close"] - last["open"])
            if last["atr"] > 0 and candle_size > self.anomaly_mult * last["atr"]:
                return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                              reason=f"anomaly candle")

        regime = detect_regime(df, idx, self.adx_threshold, self.ema_period)
        sl = max(2.0, min(last["atr_pct"] * 1.5, 8.0))

        # Выбираем стратегию по режиму
        signal_dir = None
        if regime == "bull":
            tp = sl * 2.5
            if self.bull_strategy == "momentum":
                signal_dir = momentum_signal(df, idx, self.momentum_channel, self.momentum_vol)
            elif self.bull_strategy == "rsi_extreme":
                signal_dir = rsi_extreme_signal(df, idx, self.rsi_low, self.rsi_high)
        elif regime == "range":
            tp = sl * 1.5
            if self.range_strategy == "fake_breakout":
                signal_dir = fake_breakout_signal(df, idx, self.fake_channel, self.fake_wick)
            elif self.range_strategy == "bb_bounce":
                signal_dir = bb_bounce_signal(df, idx, self.bb_rsi_os, self.bb_rsi_ob)
            elif self.range_strategy == "rsi_extreme":
                signal_dir = rsi_extreme_signal(df, idx, self.rsi_low, self.rsi_high)
        elif regime == "bear":
            tp = sl * 2.5
            if self.bear_strategy == "momentum":
                signal_dir = momentum_signal(df, idx, self.momentum_channel, self.momentum_vol)
            elif self.bear_strategy == "rsi_extreme":
                signal_dir = rsi_extreme_signal(df, idx, self.rsi_low, self.rsi_high)

        if signal_dir == "buy":
            return Signal(type=SignalType.BUY, strength=0.8, price=last["close"], symbol=symbol,
                          strategy=self.name, reason=f"{regime}:{self._strat_for(regime)}",
                          custom_sl_pct=sl, custom_tp_pct=tp)
        if signal_dir == "sell":
            return Signal(type=SignalType.SELL, strength=0.8, price=last["close"], symbol=symbol,
                          strategy=self.name, reason=f"{regime}:{self._strat_for(regime)}",
                          custom_sl_pct=sl, custom_tp_pct=tp)

        return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                      reason=f"regime={regime}, no signal")

    def _strat_for(self, regime):
        if regime == "bull": return self.bull_strategy
        if regime == "range": return self.range_strategy
        return self.bear_strategy

    def analyze(self, df, symbol):
        if len(df) < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="wait")
        df = self.precompute(df)
        return self.analyze_at(df, len(df) - 1, symbol)


# === ТЕСТ ===

def pr(label, r):
    pnl = f"+{r.total_pnl_pct:.1f}%" if r.total_pnl_pct > 0 else f"{r.total_pnl_pct:.1f}%"
    pf = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "inf"
    print(f"  {label:<55} {pnl:>8} | {r.total_trades:>3}tr | WR={r.win_rate:.0f}% | DD={r.max_drawdown_pct:.1f}% | PF={pf}")

def bt(strat, data, sym, comm=0.05):
    b = Backtester(strategy=strat, initial_balance=100.0, risk_per_trade_pct=4.0, leverage=5,
                   commission_pct=comm, slippage_pct=0.05, stop_loss_pct=5.0, take_profit_pct=10.0)
    return b.run(data, sym)


# Все комбинации для тестирования
CONFIGS = []

# Разные ADX пороги x EMA периоды
for adx in [15, 20, 25]:
    for ema in [50, 100]:
        # Разные комбинации стратегий по режимам
        for bull in ["momentum"]:
            for rng in ["fake_breakout", "bb_bounce", "rsi_extreme"]:
                for bear in ["momentum", "rsi_extreme"]:
                    # Разные параметры
                    for mom_ch in [10, 20]:
                        for fake_w in [0.3, 0.5]:
                            for anom in [True, False]:
                                label = f"ADX{adx} EMA{ema} B={bull} R={rng} Be={bear} ch{mom_ch} w{fake_w}"
                                if anom:
                                    label += " +anom"
                                CONFIGS.append({
                                    "label": label,
                                    "params": dict(
                                        adx_threshold=adx, ema_period=ema,
                                        bull_strategy=bull, range_strategy=rng, bear_strategy=bear,
                                        momentum_channel=mom_ch, fake_wick=fake_w,
                                        anomaly_filter=anom, anomaly_mult=2.0,
                                    )
                                })

# Убираем дубликаты (fake_wick не важен если range != fake_breakout)
seen = set()
unique_configs = []
for c in CONFIGS:
    p = c["params"]
    key = (p["adx_threshold"], p["ema_period"], p["bull_strategy"], p["range_strategy"],
           p["bear_strategy"], p["momentum_channel"],
           p["fake_wick"] if p["range_strategy"] == "fake_breakout" else 0,
           p["anomaly_filter"])
    if key not in seen:
        seen.add(key)
        unique_configs.append(c)


async def main():
    print("=" * 90)
    print("  META-СТРАТЕГИЯ: определение фазы рынка + переключение стратегий")
    print("  Walk-forward: Train 2024-04 — 2025-09, Test 2025-10 — 2026-04")
    print(f"  Комбинаций: {len(unique_configs)}")
    print("=" * 90)

    symbols = ["ETH/USDT", "BTC/USDT", "SOL/USDT"]
    all_results = []

    for sym in symbols:
        print(f"\nЗагрузка {sym} 4h...")
        train_data = await load_data(sym, "4h", "2024-04-01", "2025-09-30")
        test_data = await load_data(sym, "4h", "2025-10-01", "2026-04-13")
        print(f"  Train: {len(train_data)} свечей, Test: {len(test_data)} свечей")

        print(f"\n{'='*90}")
        print(f"  {sym} — TOP результаты")
        print(f"{'='*90}")

        sym_results = []
        for i, cfg in enumerate(unique_configs):
            strat = MetaStrategy(**cfg["params"])
            try:
                r_train = bt(strat, train_data, sym)
                strat2 = MetaStrategy(**cfg["params"])
                r_test = bt(strat2, test_data, sym)

                sym_results.append({
                    "label": cfg["label"],
                    "sym": sym,
                    "train_pnl": r_train.total_pnl_pct,
                    "test_pnl": r_test.total_pnl_pct,
                    "test_trades": r_test.total_trades,
                    "test_wr": r_test.win_rate,
                    "test_dd": r_test.max_drawdown_pct,
                    "test_pf": r_test.profit_factor,
                    "params": cfg["params"],
                })
            except Exception as e:
                pass

            if (i + 1) % 50 == 0:
                print(f"  ... {i+1}/{len(unique_configs)} комбинаций")

        # Сортируем по test PnL и показываем TOP-15
        sym_results.sort(key=lambda x: x["test_pnl"], reverse=True)

        print(f"\n  {'Конфигурация':<55} {'Train':>7} {'Test':>7} {'Tr':>4} {'WR':>4} {'DD':>5} {'PF':>5}")
        print(f"  {'-'*55} {'-'*7} {'-'*7} {'-'*4} {'-'*4} {'-'*5} {'-'*5}")

        for r in sym_results[:20]:
            tr = f"+{r['train_pnl']:.0f}%" if r['train_pnl'] > 0 else f"{r['train_pnl']:.0f}%"
            te = f"+{r['test_pnl']:.0f}%" if r['test_pnl'] > 0 else f"{r['test_pnl']:.0f}%"
            pf = f"{r['test_pf']:.1f}" if r['test_pf'] != float('inf') else "inf"
            # Shorten label
            lbl = r['label'][:55]
            print(f"  {lbl:<55} {tr:>7} {te:>7} {r['test_trades']:>4} {r['test_wr']:>3.0f}% {r['test_dd']:>4.0f}% {pf:>5}")

        all_results.extend(sym_results)

    # === ИТОГО: лучшие по всем монетам ===
    print(f"\n{'='*90}")
    print("  ИТОГО: TOP-20 по walk-forward test PnL (все монеты)")
    print(f"{'='*90}")

    all_results.sort(key=lambda x: x["test_pnl"], reverse=True)
    print(f"\n  {'Монета':<10} {'Конфигурация':<50} {'Train':>7} {'Test':>7} {'Tr':>4} {'WR':>4} {'DD':>5} {'PF':>5}")
    print(f"  {'-'*10} {'-'*50} {'-'*7} {'-'*7} {'-'*4} {'-'*4} {'-'*5} {'-'*5}")

    for r in all_results[:20]:
        tr = f"+{r['train_pnl']:.0f}%" if r['train_pnl'] > 0 else f"{r['train_pnl']:.0f}%"
        te = f"+{r['test_pnl']:.0f}%" if r['test_pnl'] > 0 else f"{r['test_pnl']:.0f}%"
        pf = f"{r['test_pf']:.1f}" if r['test_pf'] != float('inf') else "inf"
        lbl = r['label'][:50]
        print(f"  {r['sym']:<10} {lbl:<50} {tr:>7} {te:>7} {r['test_trades']:>4} {r['test_wr']:>3.0f}% {r['test_dd']:>4.0f}% {pf:>5}")

    # Средний PnL по стратегии для range-режима
    print(f"\n{'='*90}")
    print("  АНАЛИЗ: какая range-стратегия лучше в среднем?")
    print(f"{'='*90}")

    for rng in ["fake_breakout", "bb_bounce", "rsi_extreme"]:
        subset = [r for r in all_results if r["params"]["range_strategy"] == rng and r["test_trades"] >= 5]
        if subset:
            avg_pnl = sum(r["test_pnl"] for r in subset) / len(subset)
            avg_wr = sum(r["test_wr"] for r in subset) / len(subset)
            best = max(subset, key=lambda x: x["test_pnl"])
            print(f"\n  Range={rng}:")
            print(f"    Средний test PnL: {avg_pnl:+.1f}% ({len(subset)} конфигов)")
            print(f"    Средний WR: {avg_wr:.0f}%")
            print(f"    Лучший: {best['sym']} {best['test_pnl']:+.1f}% (train {best['train_pnl']:+.1f}%)")

    print(f"\n{'='*90}")
    print("  ГОТОВО")
    print(f"{'='*90}")


if __name__ == "__main__":
    asyncio.run(main())
