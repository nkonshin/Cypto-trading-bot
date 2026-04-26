"""
Большое исследование стратегий для боковика / отскоков / ложных пробоев.

6 стратегий x 3 монеты x 2 таймфрейма = 36+ тестов.
Период: октябрь 2025 — апрель 2026 (текущий боковик).

Стратегии:
1. Mean Reversion Channel — отскок от границ Donchian Channel
2. BB Bounce — отскок от Bollinger Bands с RSI подтверждением
3. Fake Breakout — ловим ложный пробой и торгуем в обратную сторону
4. RSI Extreme Reversal — вход при экстремальных RSI + разворотная свеча
5. Support/Resistance Grid — определяем уровни и торгуем от них
6. Keltner Reversion — отскок от Keltner Channel + Stochastic

Запуск: python -u test_range_strategies.py
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

from main import fetch_ohlcv_range, parse_date
from backtesting.backtest import Backtester
from strategies.base import BaseStrategy, Signal, SignalType


# ============================================================
# 1. Mean Reversion Channel
# ============================================================
class MeanReversionChannel(BaseStrategy):
    """Отскок от границ Donchian Channel в боковике (ADX < порога)."""
    name = "mean_reversion_ch"
    timeframe = "4h"
    min_candles = 100
    risk_category = "moderate"

    def __init__(self, channel=20, adx_max=25, rsi_period=14,
                 rsi_oversold=35, rsi_overbought=65, atr_period=14):
        self.channel = channel
        self.adx_max = adx_max
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.atr_period = atr_period

    def precompute(self, df):
        df = df.copy()
        df["dc_high"] = df["high"].rolling(self.channel).max()
        df["dc_low"] = df["low"].rolling(self.channel).min()
        df["dc_mid"] = (df["dc_high"] + df["dc_low"]) / 2
        adx = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], 14)
        df["adx"] = adx.adx()
        df["rsi"] = ta.momentum.rsi(df["close"], self.rsi_period)
        df["atr"] = ta.volatility.average_true_range(df["high"], df["low"], df["close"], self.atr_period)
        df["atr_pct"] = df["atr"] / df["close"] * 100
        return df

    def analyze_at(self, df, idx, symbol):
        if idx < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="wait")
        last = df.iloc[idx]
        if pd.isna(last["adx"]) or last["adx"] > self.adx_max:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="trend")
        price, rsi = last["close"], last["rsi"]
        sl = max(2.0, min(last["atr_pct"] * 1.5, 6.0))
        tp = sl * 1.5
        proximity = last["atr"] * 0.5
        if price <= last["dc_low"] + proximity and rsi < self.rsi_oversold:
            return Signal(type=SignalType.BUY, strength=0.8, price=price, symbol=symbol,
                          strategy=self.name, reason=f"MR long: RSI={rsi:.0f}", custom_sl_pct=sl, custom_tp_pct=tp)
        if price >= last["dc_high"] - proximity and rsi > self.rsi_overbought:
            return Signal(type=SignalType.SELL, strength=0.8, price=price, symbol=symbol,
                          strategy=self.name, reason=f"MR short: RSI={rsi:.0f}", custom_sl_pct=sl, custom_tp_pct=tp)
        return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="mid")

    def analyze(self, df, symbol):
        if len(df) < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="wait")
        df = self.precompute(df)
        return self.analyze_at(df, len(df) - 1, symbol)


# ============================================================
# 2. BB Bounce — отскок от Bollinger Bands
# ============================================================
class BBBounce(BaseStrategy):
    """Цена касается BB band + RSI подтверждает → вход на возврат к средней."""
    name = "bb_bounce"
    timeframe = "4h"
    min_candles = 100
    risk_category = "moderate"

    def __init__(self, bb_period=20, bb_std=2.0, rsi_period=14,
                 rsi_oversold=30, rsi_overbought=70, adx_max=30):
        self.bb_period = bb_period
        self.bb_std = bb_std
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.adx_max = adx_max

    def precompute(self, df):
        df = df.copy()
        bb = ta.volatility.BollingerBands(df["close"], self.bb_period, self.bb_std)
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_lower"] = bb.bollinger_lband()
        df["bb_mid"] = bb.bollinger_mavg()
        df["rsi"] = ta.momentum.rsi(df["close"], self.rsi_period)
        adx = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], 14)
        df["adx"] = adx.adx()
        df["atr"] = ta.volatility.average_true_range(df["high"], df["low"], df["close"], 14)
        df["atr_pct"] = df["atr"] / df["close"] * 100
        # Разворотная свеча (бычья: close > open после снижения)
        df["bullish_candle"] = df["close"] > df["open"]
        df["bearish_candle"] = df["close"] < df["open"]
        return df

    def analyze_at(self, df, idx, symbol):
        if idx < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="wait")
        last = df.iloc[idx]
        if pd.isna(last["adx"]) or last["adx"] > self.adx_max:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="trend")
        price, rsi = last["close"], last["rsi"]
        sl = max(1.5, min(last["atr_pct"] * 1.2, 5.0))
        tp = sl * 1.5
        if price <= last["bb_lower"] and rsi < self.rsi_oversold and last["bullish_candle"]:
            return Signal(type=SignalType.BUY, strength=0.85, price=price, symbol=symbol,
                          strategy=self.name, reason=f"BB bounce long: RSI={rsi:.0f}",
                          custom_sl_pct=sl, custom_tp_pct=tp)
        if price >= last["bb_upper"] and rsi > self.rsi_overbought and last["bearish_candle"]:
            return Signal(type=SignalType.SELL, strength=0.85, price=price, symbol=symbol,
                          strategy=self.name, reason=f"BB bounce short: RSI={rsi:.0f}",
                          custom_sl_pct=sl, custom_tp_pct=tp)
        return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="inside BB")

    def analyze(self, df, symbol):
        if len(df) < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="wait")
        df = self.precompute(df)
        return self.analyze_at(df, len(df) - 1, symbol)


# ============================================================
# 3. Fake Breakout Catcher
# ============================================================
class FakeBreakout(BaseStrategy):
    """
    Ловит ложные пробои: свеча пробила канал, но закрылась ВНУТРИ.
    Это pin bar / wick за уровнем. Входим в обратную сторону.
    """
    name = "fake_breakout"
    timeframe = "4h"
    min_candles = 100
    risk_category = "aggressive"

    def __init__(self, channel=20, wick_min_pct=0.5, adx_max=30, atr_period=14):
        self.channel = channel
        self.wick_min_pct = wick_min_pct  # минимальный размер тени за уровнем
        self.adx_max = adx_max
        self.atr_period = atr_period

    def precompute(self, df):
        df = df.copy()
        df["dc_high"] = df["high"].rolling(self.channel).max().shift(1)  # ПРЕДЫДУЩИЙ канал
        df["dc_low"] = df["low"].rolling(self.channel).min().shift(1)
        adx = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], 14)
        df["adx"] = adx.adx()
        df["atr"] = ta.volatility.average_true_range(df["high"], df["low"], df["close"], self.atr_period)
        df["atr_pct"] = df["atr"] / df["close"] * 100
        df["rsi"] = ta.momentum.rsi(df["close"], 14)
        return df

    def analyze_at(self, df, idx, symbol):
        if idx < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="wait")
        last = df.iloc[idx]
        if pd.isna(last["dc_high"]):
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="nan")
        price = last["close"]
        high, low = last["high"], last["low"]
        dc_high, dc_low = last["dc_high"], last["dc_low"]
        atr = last["atr"]
        sl = max(2.0, min(last["atr_pct"] * 1.5, 6.0))
        tp = sl * 2.0

        # Fake breakout UP: high пробил канал, но close ВНУТРИ
        if high > dc_high and price < dc_high:
            wick_above = high - max(price, last["open"])
            if wick_above > atr * (self.wick_min_pct / 100 * 100):  # wick больше порога
                return Signal(type=SignalType.SELL, strength=0.8, price=price, symbol=symbol,
                              strategy=self.name, reason=f"Fake breakout UP: high={high:.0f}>dc={dc_high:.0f}, close inside",
                              custom_sl_pct=sl, custom_tp_pct=tp)

        # Fake breakout DOWN: low пробил канал, но close ВНУТРИ
        if low < dc_low and price > dc_low:
            wick_below = min(price, last["open"]) - low
            if wick_below > atr * (self.wick_min_pct / 100 * 100):
                return Signal(type=SignalType.BUY, strength=0.8, price=price, symbol=symbol,
                              strategy=self.name, reason=f"Fake breakout DOWN: low={low:.0f}<dc={dc_low:.0f}, close inside",
                              custom_sl_pct=sl, custom_tp_pct=tp)

        return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="no fake")

    def analyze(self, df, symbol):
        if len(df) < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="wait")
        df = self.precompute(df)
        return self.analyze_at(df, len(df) - 1, symbol)


# ============================================================
# 4. RSI Extreme Reversal
# ============================================================
class RSIExtremeReversal(BaseStrategy):
    """
    Вход при экстремальных RSI (< 20 или > 80) + разворотная свеча.
    Не требует ADX фильтра — экстремальный RSI сам по себе сигнал.
    """
    name = "rsi_extreme"
    timeframe = "4h"
    min_candles = 50
    risk_category = "moderate"

    def __init__(self, rsi_period=14, rsi_extreme_low=25, rsi_extreme_high=75,
                 require_reversal_candle=True, atr_period=14):
        self.rsi_period = rsi_period
        self.rsi_extreme_low = rsi_extreme_low
        self.rsi_extreme_high = rsi_extreme_high
        self.require_reversal = require_reversal_candle
        self.atr_period = atr_period

    def precompute(self, df):
        df = df.copy()
        df["rsi"] = ta.momentum.rsi(df["close"], self.rsi_period)
        df["atr"] = ta.volatility.average_true_range(df["high"], df["low"], df["close"], self.atr_period)
        df["atr_pct"] = df["atr"] / df["close"] * 100
        df["bullish_reversal"] = (df["close"] > df["open"]) & (df["close"].shift(1) < df["open"].shift(1))
        df["bearish_reversal"] = (df["close"] < df["open"]) & (df["close"].shift(1) > df["open"].shift(1))
        # EMA для направления
        df["ema50"] = ta.trend.ema_indicator(df["close"], 50)
        return df

    def analyze_at(self, df, idx, symbol):
        if idx < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="wait")
        last = df.iloc[idx]
        rsi = last["rsi"]
        if pd.isna(rsi):
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="nan")
        sl = max(2.0, min(last["atr_pct"] * 1.5, 6.0))
        tp = sl * 2.0

        if rsi < self.rsi_extreme_low:
            if not self.require_reversal or last["bullish_reversal"]:
                return Signal(type=SignalType.BUY, strength=0.9, price=last["close"], symbol=symbol,
                              strategy=self.name, reason=f"RSI extreme low={rsi:.0f} + reversal",
                              custom_sl_pct=sl, custom_tp_pct=tp)

        if rsi > self.rsi_extreme_high:
            if not self.require_reversal or last["bearish_reversal"]:
                return Signal(type=SignalType.SELL, strength=0.9, price=last["close"], symbol=symbol,
                              strategy=self.name, reason=f"RSI extreme high={rsi:.0f} + reversal",
                              custom_sl_pct=sl, custom_tp_pct=tp)

        return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason=f"RSI={rsi:.0f}")

    def analyze(self, df, symbol):
        if len(df) < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="wait")
        df = self.precompute(df)
        return self.analyze_at(df, len(df) - 1, symbol)


# ============================================================
# 5. Support/Resistance Bounce
# ============================================================
class SRBounce(BaseStrategy):
    """
    Определяем уровни поддержки/сопротивления по swing high/low.
    Торгуем отскок от уровня с RSI + volume подтверждением.
    """
    name = "sr_bounce"
    timeframe = "4h"
    min_candles = 100
    risk_category = "moderate"

    def __init__(self, lookback=50, proximity_atr=0.5, min_touches=2,
                 rsi_period=14, atr_period=14):
        self.lookback = lookback
        self.proximity_atr = proximity_atr
        self.min_touches = min_touches
        self.rsi_period = rsi_period
        self.atr_period = atr_period

    def _find_levels(self, df, idx):
        """Находит уровни S/R по swing high/low."""
        start = max(0, idx - self.lookback)
        segment = df.iloc[start:idx+1]
        atr = segment["atr"].iloc[-1] if "atr" in segment else 1.0

        levels = []
        for i in range(2, len(segment) - 2):
            row = segment.iloc[i]
            # Swing high
            if row["high"] >= segment.iloc[i-1]["high"] and row["high"] >= segment.iloc[i-2]["high"] and \
               row["high"] >= segment.iloc[i+1]["high"] and row["high"] >= segment.iloc[i+2]["high"]:
                levels.append(("resistance", row["high"]))
            # Swing low
            if row["low"] <= segment.iloc[i-1]["low"] and row["low"] <= segment.iloc[i-2]["low"] and \
               row["low"] <= segment.iloc[i+1]["low"] and row["low"] <= segment.iloc[i+2]["low"]:
                levels.append(("support", row["low"]))

        # Кластеризуем близкие уровни
        if not levels:
            return []
        clustered = []
        used = set()
        for i, (typ, lvl) in enumerate(levels):
            if i in used:
                continue
            cluster = [lvl]
            for j, (typ2, lvl2) in enumerate(levels[i+1:], i+1):
                if abs(lvl2 - lvl) < atr * self.proximity_atr:
                    cluster.append(lvl2)
                    used.add(j)
            if len(cluster) >= self.min_touches:
                avg = sum(cluster) / len(cluster)
                clustered.append({"level": avg, "touches": len(cluster), "type": typ})
        return clustered

    def precompute(self, df):
        df = df.copy()
        df["rsi"] = ta.momentum.rsi(df["close"], self.rsi_period)
        df["atr"] = ta.volatility.average_true_range(df["high"], df["low"], df["close"], self.atr_period)
        df["atr_pct"] = df["atr"] / df["close"] * 100
        df["vol_sma"] = df["volume"].rolling(20).mean()
        return df

    def analyze_at(self, df, idx, symbol):
        if idx < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="wait")
        last = df.iloc[idx]
        price = last["close"]
        atr = last["atr"]

        levels = self._find_levels(df, idx)
        if not levels:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="no levels")

        sl = max(2.0, min(last["atr_pct"] * 1.5, 6.0))
        tp = sl * 1.5

        for lvl_info in levels:
            lvl = lvl_info["level"]
            dist = abs(price - lvl)
            if dist < atr * self.proximity_atr:
                if price > lvl and lvl_info["type"] == "support" and last["rsi"] < 45:
                    return Signal(type=SignalType.BUY, strength=0.75, price=price, symbol=symbol,
                                  strategy=self.name,
                                  reason=f"SR bounce long: support {lvl:.0f} ({lvl_info['touches']}x)",
                                  custom_sl_pct=sl, custom_tp_pct=tp)
                if price < lvl and lvl_info["type"] == "resistance" and last["rsi"] > 55:
                    return Signal(type=SignalType.SELL, strength=0.75, price=price, symbol=symbol,
                                  strategy=self.name,
                                  reason=f"SR bounce short: resistance {lvl:.0f} ({lvl_info['touches']}x)",
                                  custom_sl_pct=sl, custom_tp_pct=tp)

        return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="not near level")

    def analyze(self, df, symbol):
        if len(df) < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="wait")
        df = self.precompute(df)
        return self.analyze_at(df, len(df) - 1, symbol)


# ============================================================
# 6. Keltner Reversion + Stochastic
# ============================================================
class KeltnerReversion(BaseStrategy):
    """Отскок от Keltner Channel + Stochastic подтверждение."""
    name = "keltner_reversion"
    timeframe = "4h"
    min_candles = 100
    risk_category = "moderate"

    def __init__(self, kc_period=20, kc_mult=2.0, stoch_period=14,
                 stoch_smooth=3, stoch_oversold=20, stoch_overbought=80,
                 adx_max=30):
        self.kc_period = kc_period
        self.kc_mult = kc_mult
        self.stoch_period = stoch_period
        self.stoch_smooth = stoch_smooth
        self.stoch_oversold = stoch_oversold
        self.stoch_overbought = stoch_overbought
        self.adx_max = adx_max

    def precompute(self, df):
        df = df.copy()
        ema = ta.trend.ema_indicator(df["close"], self.kc_period)
        atr = ta.volatility.average_true_range(df["high"], df["low"], df["close"], self.kc_period)
        df["kc_upper"] = ema + self.kc_mult * atr
        df["kc_lower"] = ema - self.kc_mult * atr
        df["kc_mid"] = ema
        stoch = ta.momentum.StochasticOscillator(df["high"], df["low"], df["close"],
                                                   self.stoch_period, self.stoch_smooth)
        df["stoch_k"] = stoch.stoch()
        df["stoch_d"] = stoch.stoch_signal()
        adx_ind = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], 14)
        df["adx"] = adx_ind.adx()
        df["atr"] = atr
        df["atr_pct"] = atr / df["close"] * 100
        return df

    def analyze_at(self, df, idx, symbol):
        if idx < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="wait")
        last = df.iloc[idx]
        if pd.isna(last["stoch_k"]):
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="nan")
        if last["adx"] > self.adx_max:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="trend")
        price = last["close"]
        sl = max(2.0, min(last["atr_pct"] * 1.5, 6.0))
        tp = sl * 1.5

        if price <= last["kc_lower"] and last["stoch_k"] < self.stoch_oversold:
            return Signal(type=SignalType.BUY, strength=0.8, price=price, symbol=symbol,
                          strategy=self.name, reason=f"Keltner bounce long: Stoch={last['stoch_k']:.0f}",
                          custom_sl_pct=sl, custom_tp_pct=tp)
        if price >= last["kc_upper"] and last["stoch_k"] > self.stoch_overbought:
            return Signal(type=SignalType.SELL, strength=0.8, price=price, symbol=symbol,
                          strategy=self.name, reason=f"Keltner bounce short: Stoch={last['stoch_k']:.0f}",
                          custom_sl_pct=sl, custom_tp_pct=tp)

        return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="inside KC")

    def analyze(self, df, symbol):
        if len(df) < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="wait")
        df = self.precompute(df)
        return self.analyze_at(df, len(df) - 1, symbol)


# ============================================================
# ТЕСТИРОВАНИЕ
# ============================================================

def print_result(label, r):
    pnl = f"+{r.total_pnl_pct:.1f}%" if r.total_pnl_pct > 0 else f"{r.total_pnl_pct:.1f}%"
    pf = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "inf"
    print(f"  {label:<45} {pnl:>8} | {r.total_trades:>3}tr | WR={r.win_rate:.0f}% | DD={r.max_drawdown_pct:.1f}% | PF={pf}")


def run_bt(strategy, data, symbol, sl=4.0, tp=6.0, commission=0.05, risk=4.0):
    bt = Backtester(
        strategy=strategy, initial_balance=100.0,
        risk_per_trade_pct=risk, leverage=5,
        commission_pct=commission, slippage_pct=0.05,
        stop_loss_pct=sl, take_profit_pct=tp,
    )
    return bt.run(data, symbol)


async def main():
    print("=" * 80)
    print("  ИССЛЕДОВАНИЕ СТРАТЕГИЙ ДЛЯ БОКОВИКА / ОТСКОКОВ / ЛОЖНЫХ ПРОБОЕВ")
    print("=" * 80)

    # Загрузка данных
    print("\nЗагрузка данных (окт 2025 — апр 2026)...")
    symbols_4h = {}
    symbols_15m = {}
    for sym in ["ETH/USDT", "BTC/USDT", "SOL/USDT"]:
        d4h = await fetch_ohlcv_range(sym, "4h", since=parse_date("2025-10-01"), until=parse_date("2026-04-13"))
        d15m = await fetch_ohlcv_range(sym, "15m", since=parse_date("2025-10-01"), until=parse_date("2026-04-13"))
        symbols_4h[sym] = d4h
        symbols_15m[sym] = d15m
        print(f"  {sym}: {len(d4h)} (4h), {len(d15m)} (15m)")

    # Стратегии с вариациями параметров
    strategies_4h = [
        # Mean Reversion Channel
        ("MR ch=20 adx<25", MeanReversionChannel(channel=20, adx_max=25), 4.0, 6.0),
        ("MR ch=30 adx<25", MeanReversionChannel(channel=30, adx_max=25), 4.0, 6.0),
        ("MR ch=20 adx<30", MeanReversionChannel(channel=20, adx_max=30), 4.0, 6.0),
        ("MR ch=30 adx<30", MeanReversionChannel(channel=30, adx_max=30), 4.0, 6.0),
        ("MR ch=30 adx<30 rsi=30/70", MeanReversionChannel(channel=30, adx_max=30, rsi_oversold=30, rsi_overbought=70), 4.0, 6.0),
        # BB Bounce
        ("BB bounce std=2.0 rsi=30/70", BBBounce(bb_std=2.0, rsi_oversold=30, rsi_overbought=70), 3.0, 5.0),
        ("BB bounce std=2.0 rsi=25/75", BBBounce(bb_std=2.0, rsi_oversold=25, rsi_overbought=75), 3.0, 5.0),
        ("BB bounce std=1.5 rsi=30/70", BBBounce(bb_std=1.5, rsi_oversold=30, rsi_overbought=70), 3.0, 5.0),
        ("BB bounce std=2.0 adx<25", BBBounce(bb_std=2.0, adx_max=25, rsi_oversold=30, rsi_overbought=70), 3.0, 5.0),
        # Fake Breakout
        ("Fake breakout ch=20 wick=0.3", FakeBreakout(channel=20, wick_min_pct=0.3), 4.0, 8.0),
        ("Fake breakout ch=20 wick=0.5", FakeBreakout(channel=20, wick_min_pct=0.5), 4.0, 8.0),
        ("Fake breakout ch=30 wick=0.3", FakeBreakout(channel=30, wick_min_pct=0.3), 4.0, 8.0),
        # RSI Extreme Reversal
        ("RSI extreme 25/75 + reversal", RSIExtremeReversal(rsi_extreme_low=25, rsi_extreme_high=75), 4.0, 8.0),
        ("RSI extreme 20/80 + reversal", RSIExtremeReversal(rsi_extreme_low=20, rsi_extreme_high=80), 4.0, 8.0),
        ("RSI extreme 25/75 no reversal", RSIExtremeReversal(rsi_extreme_low=25, rsi_extreme_high=75, require_reversal_candle=False), 4.0, 8.0),
        ("RSI extreme 30/70 + reversal", RSIExtremeReversal(rsi_extreme_low=30, rsi_extreme_high=70), 4.0, 8.0),
        # SR Bounce
        ("SR bounce touches=2", SRBounce(min_touches=2, proximity_atr=0.5), 4.0, 6.0),
        ("SR bounce touches=3", SRBounce(min_touches=3, proximity_atr=0.5), 4.0, 6.0),
        ("SR bounce touches=2 prox=0.3", SRBounce(min_touches=2, proximity_atr=0.3), 4.0, 6.0),
        # Keltner Reversion
        ("Keltner kc=2.0 stoch=20/80", KeltnerReversion(kc_mult=2.0, stoch_oversold=20, stoch_overbought=80), 4.0, 6.0),
        ("Keltner kc=1.5 stoch=20/80", KeltnerReversion(kc_mult=1.5, stoch_oversold=20, stoch_overbought=80), 4.0, 6.0),
        ("Keltner kc=2.0 stoch=25/75", KeltnerReversion(kc_mult=2.0, stoch_oversold=25, stoch_overbought=75), 4.0, 6.0),
        ("Keltner kc=2.0 adx<25", KeltnerReversion(kc_mult=2.0, adx_max=25), 4.0, 6.0),
    ]

    # Тестируем на всех монетах
    for sym in ["ETH/USDT", "BTC/USDT", "SOL/USDT"]:
        print(f"\n{'='*80}")
        print(f"  {sym} — 4h")
        print(f"{'='*80}")
        data = symbols_4h[sym]
        for label, strat, sl, tp in strategies_4h:
            strat.timeframe = "4h"
            r = run_bt(strat, data, sym, sl, tp)
            if r.total_trades >= 2:
                print_result(f"{label}", r)

    # 15m версии лучших стратегий
    strategies_15m = [
        ("MR ch=20 adx<25", MeanReversionChannel(channel=20, adx_max=25), 2.0, 3.0),
        ("MR ch=30 adx<30", MeanReversionChannel(channel=30, adx_max=30), 2.0, 3.0),
        ("BB bounce std=2.0", BBBounce(bb_std=2.0, rsi_oversold=30, rsi_overbought=70), 2.0, 3.0),
        ("BB bounce std=1.5", BBBounce(bb_std=1.5, rsi_oversold=30, rsi_overbought=70), 2.0, 3.0),
        ("RSI extreme 25/75", RSIExtremeReversal(rsi_extreme_low=25, rsi_extreme_high=75), 2.0, 4.0),
        ("RSI extreme 30/70", RSIExtremeReversal(rsi_extreme_low=30, rsi_extreme_high=70), 2.0, 4.0),
        ("Keltner kc=2.0", KeltnerReversion(kc_mult=2.0), 2.0, 3.0),
        ("Keltner kc=1.5", KeltnerReversion(kc_mult=1.5), 2.0, 3.0),
    ]

    for sym in ["ETH/USDT", "BTC/USDT", "SOL/USDT"]:
        print(f"\n{'='*80}")
        print(f"  {sym} — 15m (maker fee 0.02%)")
        print(f"{'='*80}")
        data = symbols_15m[sym]
        for label, strat, sl, tp in strategies_15m:
            strat.timeframe = "15m"
            r = run_bt(strat, data, sym, sl, tp, commission=0.02)
            if r.total_trades >= 3:
                print_result(f"{label}", r)

    # =========================================================
    # ИТОГ: TOP-10
    # =========================================================
    print(f"\n{'='*80}")
    print("  Тест завершён.")
    print(f"{'='*80}")


if __name__ == "__main__":
    asyncio.run(main())
