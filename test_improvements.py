"""
Тест 4 вариантов улучшения пробойных стратегий.

Проблема: на текущем рынке (боковик) пробои часто ложные.
Тестируем на ETH/USDT, период включающий текущий боковик.

Варианты:
1. Mean Reversion — торговля отскоков вместо пробоев
2. Regime Filter — фильтр фазы рынка (не торгуем пробои в боковике)
3. Pullback Entry — вход после отката к уровню пробоя
4. Anomaly Filter — игнорируем аномально большие свечи

Запуск: python test_improvements.py
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
from strategies.momentum_breakout import MomentumBreakoutStrategy
from strategies.micro_breakout import MicroBreakoutStrategy
from backtesting.optimized_params import get_optimized_strategy, get_optimized_backtest_params


# ============================================================
# ВАРИАНТ 1: Mean Reversion — торговля отскоков от границ канала
# ============================================================

class MeanReversionChannel(BaseStrategy):
    """
    Вместо пробоя канала — торгуем возврат от границ к середине.
    Цена подходит к верхней границе → шорт (ожидаем возврат).
    Цена подходит к нижней границе → лонг (ожидаем возврат).
    ADX < 25 = боковик (наш рабочий режим).
    """
    name = "mean_reversion_channel"
    description = "Mean Reversion: отскок от границ канала"
    timeframe = "4h"
    min_candles = 100
    risk_category = "moderate"

    def __init__(self, channel_period=20, atr_period=14, adx_period=14,
                 adx_max=25, proximity_pct=1.0, rsi_period=14):
        self.channel_period = channel_period
        self.atr_period = atr_period
        self.adx_period = adx_period
        self.adx_max = adx_max
        self.proximity_pct = proximity_pct  # % от границы для входа
        self.rsi_period = rsi_period

    def precompute(self, df):
        df = df.copy()
        df["dc_high"] = df["high"].rolling(self.channel_period).max()
        df["dc_low"] = df["low"].rolling(self.channel_period).min()
        df["dc_mid"] = (df["dc_high"] + df["dc_low"]) / 2
        df["atr"] = ta.volatility.average_true_range(df["high"], df["low"], df["close"], self.atr_period)
        df["atr_pct"] = df["atr"] / df["close"] * 100
        adx = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], self.adx_period)
        df["adx"] = adx.adx()
        df["rsi"] = ta.momentum.rsi(df["close"], self.rsi_period)
        df["vol_sma"] = df["volume"].rolling(20).mean()
        return df

    def analyze_at(self, df, idx, symbol):
        if idx < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="Недостаточно данных")
        last = df.iloc[idx]
        if pd.isna(last["adx"]):
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="NaN")

        price = last["close"]
        dc_high = last["dc_high"]
        dc_low = last["dc_low"]
        adx = last["adx"]
        rsi = last["rsi"]

        # Только в боковике
        if adx > self.adx_max:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason=f"ADX={adx:.0f} > {self.adx_max} (тренд)")

        proximity = self.proximity_pct / 100
        sl_pct = last["atr_pct"] * 1.5
        sl_pct = max(2.0, min(sl_pct, 8.0))
        tp_pct = sl_pct * 1.5  # TP = до середины канала

        # LONG: цена у нижней границы + RSI перепродан
        if price <= dc_low * (1 + proximity) and rsi < 40:
            return Signal(type=SignalType.BUY, strength=0.8, price=price,
                          symbol=symbol, strategy=self.name,
                          reason=f"Отскок от дна канала, RSI={rsi:.0f}, ADX={adx:.0f}",
                          custom_sl_pct=sl_pct, custom_tp_pct=tp_pct)

        # SHORT: цена у верхней границы + RSI перекуплен
        if price >= dc_high * (1 - proximity) and rsi > 60:
            return Signal(type=SignalType.SELL, strength=0.8, price=price,
                          symbol=symbol, strategy=self.name,
                          reason=f"Отскок от верха канала, RSI={rsi:.0f}, ADX={adx:.0f}",
                          custom_sl_pct=sl_pct, custom_tp_pct=tp_pct)

        return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                      reason=f"Цена в середине канала, ADX={adx:.0f}")

    def analyze(self, df, symbol):
        if len(df) < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="Мало данных")
        df = self.precompute(df)
        return self.analyze_at(df, len(df) - 1, symbol)


# ============================================================
# ВАРИАНТ 2: Regime Filter — пробой только в тренде
# ============================================================

class MomentumWithRegimeFilter(MomentumBreakoutStrategy):
    """
    Оригинальный Momentum Breakout + ADX фильтр:
    торгуем пробои ТОЛЬКО когда ADX > 20 (тренд).
    В боковике (ADX < 20) — пропускаем.
    """
    name = "momentum_regime_filter"

    def __init__(self, adx_min=20, adx_period=14, **kwargs):
        super().__init__(**kwargs)
        self.adx_min = adx_min
        self.adx_period_regime = adx_period

    def precompute(self, df):
        df = super().precompute(df)
        adx = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], self.adx_period_regime)
        df["adx_regime"] = adx.adx()
        return df

    def analyze_at(self, df, idx, symbol):
        if idx < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="Мало данных")
        last = df.iloc[idx]
        if pd.notna(last.get("adx_regime")) and last["adx_regime"] < self.adx_min:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason=f"ADX={last['adx_regime']:.0f} < {self.adx_min} (боковик, пропускаем)")
        return super().analyze_at(df, idx, symbol)


class MicroWithRegimeFilter(MicroBreakoutStrategy):
    """Micro Breakout + ADX фильтр: пробой только в тренде."""
    name = "micro_regime_filter"

    def __init__(self, adx_min=20, **kwargs):
        super().__init__(**kwargs)
        self.adx_min_regime = adx_min

    def analyze_at(self, df, idx, symbol):
        if idx < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="Мало данных")
        last = df.iloc[idx]
        if pd.notna(last.get("adx")) and last["adx"] < self.adx_min_regime:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason=f"ADX={last['adx']:.0f} < {self.adx_min_regime} (боковик)")
        return super().analyze_at(df, idx, symbol)


# ============================================================
# ВАРИАНТ 3: Pullback Entry — вход после отката к уровню
# ============================================================

class MomentumPullback(MomentumBreakoutStrategy):
    """
    Вместо входа сразу на пробое — ждём откат к уровню пробоя.
    Пробой вверх → ждём откат к бывшей верхней границе → входим.
    Это отсекает ложные пробои (цена пробила и сразу вернулась).
    """
    name = "momentum_pullback"

    def __init__(self, pullback_tolerance_pct=0.5, **kwargs):
        super().__init__(**kwargs)
        self.pullback_tolerance = pullback_tolerance_pct / 100
        self._pending_breakout = None  # {side, level, candles_since}

    def precompute(self, df):
        df = super().precompute(df)
        # Запоминаем предыдущие границы канала
        df["prev_dc_high"] = df["dc_high"].shift(1) if "dc_high" in df else None
        df["prev_dc_low"] = df["dc_low"].shift(1) if "dc_low" in df else None
        return df

    def analyze_at(self, df, idx, symbol):
        if idx < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="Мало данных")

        last = df.iloc[idx]
        price = last["close"]

        # Получаем оригинальный сигнал
        original = super().analyze_at(df, idx, symbol)

        # Если оригинал даёт пробой — не входим сразу, запоминаем
        if original.type == SignalType.BUY:
            self._pending_breakout = {"side": "buy", "level": last.get("dc_high", price), "candles": 0}
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason=f"Пробой UP зафиксирован, ждём pullback к {last.get('dc_high', 0):.0f}")

        if original.type == SignalType.SELL:
            self._pending_breakout = {"side": "sell", "level": last.get("dc_low", price), "candles": 0}
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason=f"Пробой DOWN зафиксирован, ждём pullback к {last.get('dc_low', 0):.0f}")

        # Проверяем pending pullback
        if self._pending_breakout:
            self._pending_breakout["candles"] += 1

            # Таймаут: если за 10 свечей не было pullback — отменяем
            if self._pending_breakout["candles"] > 10:
                self._pending_breakout = None
                return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                              reason="Pullback timeout")

            level = self._pending_breakout["level"]
            tolerance = level * self.pullback_tolerance

            if self._pending_breakout["side"] == "buy":
                # Цена вернулась к уровню пробоя (pullback) и снова пошла вверх
                if price <= level + tolerance and price >= level - tolerance:
                    self._pending_breakout = None
                    sl_pct = original.custom_sl_pct or 8.0
                    tp_pct = original.custom_tp_pct or 7.0
                    return Signal(type=SignalType.BUY, strength=0.85, price=price,
                                  symbol=symbol, strategy=self.name,
                                  reason=f"Pullback entry LONG: цена вернулась к {level:.0f}",
                                  custom_sl_pct=sl_pct, custom_tp_pct=tp_pct)

            elif self._pending_breakout["side"] == "sell":
                if price >= level - tolerance and price <= level + tolerance:
                    self._pending_breakout = None
                    sl_pct = original.custom_sl_pct or 8.0
                    tp_pct = original.custom_tp_pct or 7.0
                    return Signal(type=SignalType.SELL, strength=0.85, price=price,
                                  symbol=symbol, strategy=self.name,
                                  reason=f"Pullback entry SHORT: цена вернулась к {level:.0f}",
                                  custom_sl_pct=sl_pct, custom_tp_pct=tp_pct)

        return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="Ожидание")

    def analyze(self, df, symbol):
        # Reset state for backtest
        self._pending_breakout = None
        if len(df) < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="Мало данных")
        df = self.precompute(df)
        return self.analyze_at(df, len(df) - 1, symbol)


# ============================================================
# ВАРИАНТ 4: Anomaly Filter — игнорируем аномальные свечи
# ============================================================

class MomentumNoAnomaly(MomentumBreakoutStrategy):
    """
    Momentum Breakout + фильтр аномальных свечей:
    если текущая свеча > 2.5x ATR — не входим (новостной спайк).
    Ждём следующую свечу для подтверждения.
    """
    name = "momentum_no_anomaly"

    def __init__(self, anomaly_mult=2.5, **kwargs):
        super().__init__(**kwargs)
        self.anomaly_mult = anomaly_mult

    def analyze_at(self, df, idx, symbol):
        if idx < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="Мало данных")

        last = df.iloc[idx]
        candle_size = abs(last["close"] - last["open"])
        atr = last.get("atr", 0)

        if atr > 0 and candle_size > self.anomaly_mult * atr:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason=f"Аномальная свеча: size={candle_size:.0f} > {self.anomaly_mult}xATR={atr*self.anomaly_mult:.0f}")

        return super().analyze_at(df, idx, symbol)


class MicroNoAnomaly(MicroBreakoutStrategy):
    """Micro Breakout + фильтр аномальных свечей."""
    name = "micro_no_anomaly"

    def __init__(self, anomaly_mult=2.5, **kwargs):
        super().__init__(**kwargs)
        self.anomaly_mult = anomaly_mult

    def analyze_at(self, df, idx, symbol):
        if idx < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="Мало данных")

        last = df.iloc[idx]
        candle_size = abs(last["close"] - last["open"])
        atr = last.get("atr", 0)

        if atr > 0 and candle_size > self.anomaly_mult * atr:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason=f"Аномальная свеча: пропускаем")

        return super().analyze_at(df, idx, symbol)


# ============================================================
# ТЕСТИРОВАНИЕ
# ============================================================

def print_result(label, r):
    pnl = f"+{r.total_pnl_pct:.1f}%" if r.total_pnl_pct > 0 else f"{r.total_pnl_pct:.1f}%"
    pf = f"{r.profit_factor:.2f}" if r.profit_factor != float("inf") else "inf"
    print(f"  {label:<40} PnL={pnl:>8} | {r.total_trades:>3} trades | WR={r.win_rate:.0f}% | DD={r.max_drawdown_pct:.1f}% | PF={pf}")


def run_bt(strategy, data, symbol, sl_pct, tp_pct, commission=0.05):
    bt = Backtester(
        strategy=strategy, initial_balance=100.0,
        risk_per_trade_pct=4.0, leverage=5,
        commission_pct=commission, slippage_pct=0.05,
        stop_loss_pct=sl_pct, take_profit_pct=tp_pct,
    )
    return bt.run(data, symbol)


async def main():
    print("=" * 70)
    print("  ТЕСТ УЛУЧШЕНИЙ ПРОБОЙНЫХ СТРАТЕГИЙ")
    print("=" * 70)

    # Загрузка данных
    print("\nЗагрузка данных...")
    # Тестируем на периоде включающем текущий боковик
    # Walk-forward: train до 2025-09, test с 2025-10
    eth_4h = await fetch_ohlcv_range("ETH/USDT", "4h",
        since=parse_date("2025-10-01"), until=parse_date("2026-04-12"))
    eth_15m = await fetch_ohlcv_range("ETH/USDT", "15m",
        since=parse_date("2025-10-01"), until=parse_date("2026-04-12"))
    print(f"  ETH 4h: {len(eth_4h)} свечей")
    print(f"  ETH 15m: {len(eth_15m)} свечей")

    symbol = "ETH/USDT"

    # =========================================================
    # BASELINE: оригинальные стратегии с текущими параметрами
    # =========================================================
    print("\n" + "=" * 70)
    print("  BASELINE (текущие стратегии)")
    print("=" * 70)

    # Momentum 4h
    s_mom = get_optimized_strategy("momentum_breakout", symbol)
    bp_mom = get_optimized_backtest_params("momentum_breakout", symbol)
    r_mom = run_bt(s_mom, eth_4h, symbol, bp_mom["stop_loss_pct"], bp_mom["take_profit_pct"])
    print_result("Momentum Breakout 4h (original)", r_mom)

    # Micro 15m
    s_micro = get_optimized_strategy("micro_breakout", symbol)
    s_micro.timeframe = "15m"
    bp_micro = get_optimized_backtest_params("micro_breakout", symbol)
    r_micro = run_bt(s_micro, eth_15m, symbol, bp_micro["stop_loss_pct"], bp_micro["take_profit_pct"], commission=0.02)
    print_result("Micro Breakout 15m (original)", r_micro)

    # =========================================================
    # ВАРИАНТ 1: Mean Reversion
    # =========================================================
    print("\n" + "=" * 70)
    print("  ВАРИАНТ 1: Mean Reversion (отскок от границ канала)")
    print("=" * 70)
    print("  Идея: торгуем возврат к середине канала вместо пробоя")
    print()

    for adx_max in [20, 25, 30]:
        for channel in [15, 20, 30]:
            s = MeanReversionChannel(channel_period=channel, adx_max=adx_max)
            r = run_bt(s, eth_4h, symbol, 4.0, 6.0)
            if r.total_trades >= 3:
                print_result(f"  MR ch={channel} adx<{adx_max}", r)

    # 15m версия
    print()
    for adx_max in [20, 25, 30]:
        s = MeanReversionChannel(channel_period=20, adx_max=adx_max)
        s.timeframe = "15m"
        r = run_bt(s, eth_15m, symbol, 2.0, 4.0, commission=0.02)
        if r.total_trades >= 3:
            print_result(f"  MR 15m adx<{adx_max}", r)

    # =========================================================
    # ВАРИАНТ 2: Regime Filter
    # =========================================================
    print("\n" + "=" * 70)
    print("  ВАРИАНТ 2: Regime Filter (пробой только в тренде)")
    print("=" * 70)
    print("  Идея: не торгуем пробои когда ADX низкий (боковик)")
    print()

    for adx_min in [15, 20, 25, 30]:
        s = MomentumWithRegimeFilter(
            adx_min=adx_min,
            channel_period=10, atr_period=14, atr_sl_mult=1.0,
            rr_ratio=3.5, volume_mult=1.75,
        )
        r = run_bt(s, eth_4h, symbol, bp_mom["stop_loss_pct"], bp_mom["take_profit_pct"])
        print_result(f"  Momentum + ADX>{adx_min}", r)

    print()
    for adx_min in [15, 20, 25]:
        s = MicroWithRegimeFilter(
            adx_min=adx_min,
            atr_period=14, atr_lookback=75, atr_percentile=35.0,
            channel_period=10, ema_trend=70, min_squeeze_bars=8,
            atr_sl_mult=2.0, rr_ratio=3.5, volume_breakout_mult=2.0,
        )
        s.timeframe = "15m"
        r = run_bt(s, eth_15m, symbol, bp_micro["stop_loss_pct"], bp_micro["take_profit_pct"], commission=0.02)
        print_result(f"  Micro + ADX>{adx_min}", r)

    # =========================================================
    # ВАРИАНТ 3: Pullback Entry
    # =========================================================
    print("\n" + "=" * 70)
    print("  ВАРИАНТ 3: Pullback Entry (вход после отката)")
    print("=" * 70)
    print("  Идея: не входим на пробое, а ждём возврат к уровню")
    print()

    for tolerance in [0.3, 0.5, 1.0, 1.5]:
        s = MomentumPullback(
            pullback_tolerance_pct=tolerance,
            channel_period=10, atr_period=14, atr_sl_mult=1.0,
            rr_ratio=3.5, volume_mult=1.75,
        )
        r = run_bt(s, eth_4h, symbol, bp_mom["stop_loss_pct"], bp_mom["take_profit_pct"])
        print_result(f"  Pullback tolerance={tolerance}%", r)

    # =========================================================
    # ВАРИАНТ 4: Anomaly Filter
    # =========================================================
    print("\n" + "=" * 70)
    print("  ВАРИАНТ 4: Anomaly Filter (игнорируем аномальные свечи)")
    print("=" * 70)
    print("  Идея: если свеча > N*ATR — не входим (новостной спайк)")
    print()

    for mult in [1.5, 2.0, 2.5, 3.0]:
        s = MomentumNoAnomaly(
            anomaly_mult=mult,
            channel_period=10, atr_period=14, atr_sl_mult=1.0,
            rr_ratio=3.5, volume_mult=1.75,
        )
        r = run_bt(s, eth_4h, symbol, bp_mom["stop_loss_pct"], bp_mom["take_profit_pct"])
        print_result(f"  Momentum no_anomaly>{mult}xATR", r)

    print()
    for mult in [1.5, 2.0, 2.5, 3.0]:
        s = MicroNoAnomaly(
            anomaly_mult=mult,
            atr_period=14, atr_lookback=75, atr_percentile=35.0,
            channel_period=10, ema_trend=70, min_squeeze_bars=8,
            atr_sl_mult=2.0, rr_ratio=3.5, volume_breakout_mult=2.0,
        )
        s.timeframe = "15m"
        r = run_bt(s, eth_15m, symbol, bp_micro["stop_loss_pct"], bp_micro["take_profit_pct"], commission=0.02)
        print_result(f"  Micro no_anomaly>{mult}xATR", r)

    # =========================================================
    # КОМБО: Regime + Anomaly (лучшее из обоих)
    # =========================================================
    print("\n" + "=" * 70)
    print("  COMBO: Regime Filter + Anomaly Filter")
    print("=" * 70)

    # Momentum 4h с обоими фильтрами
    class MomentumCombo(MomentumBreakoutStrategy):
        name = "momentum_combo"
        def __init__(self, adx_min=20, anomaly_mult=2.0, **kwargs):
            super().__init__(**kwargs)
            self.adx_min = adx_min
            self.anomaly_mult = anomaly_mult
        def precompute(self, df):
            df = super().precompute(df)
            adx = ta.trend.ADXIndicator(df["high"], df["low"], df["close"], 14)
            df["adx_combo"] = adx.adx()
            return df
        def analyze_at(self, df, idx, symbol):
            if idx < self.min_candles:
                return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name, reason="Мало данных")
            last = df.iloc[idx]
            if pd.notna(last.get("adx_combo")) and last["adx_combo"] < self.adx_min:
                return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                              reason=f"ADX={last['adx_combo']:.0f} (боковик)")
            candle_size = abs(last["close"] - last["open"])
            atr = last.get("atr", 0)
            if atr > 0 and candle_size > self.anomaly_mult * atr:
                return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                              reason=f"Аномальная свеча")
            return super().analyze_at(df, idx, symbol)

    for adx_min in [20, 25]:
        for anom in [2.0, 2.5]:
            s = MomentumCombo(
                adx_min=adx_min, anomaly_mult=anom,
                channel_period=10, atr_period=14, atr_sl_mult=1.0,
                rr_ratio=3.5, volume_mult=1.75,
            )
            r = run_bt(s, eth_4h, symbol, bp_mom["stop_loss_pct"], bp_mom["take_profit_pct"])
            print_result(f"  Combo ADX>{adx_min} + anom>{anom}x", r)

    print("\n" + "=" * 70)
    print("  ГОТОВО")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
