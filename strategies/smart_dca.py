"""
Стратегия Smart DCA (Dollar Cost Averaging) — усреднение с умным входом.

Вместо слепого DCA по времени, использует технические индикаторы
для определения оптимальных точек входа. Докупает при просадках,
увеличивая позицию на сильных уровнях поддержки.

Подходит для: долгосрочного накопления, медвежьих рынков.
Риск: Консервативный.
"""

import ta
import pandas as pd
from strategies.base import BaseStrategy, Signal, SignalType


class SmartDcaStrategy(BaseStrategy):
    name = "smart_dca"
    description = "Умное усреднение с техническим анализом"
    timeframe = "4h"
    min_candles = 100
    risk_category = "conservative"

    def __init__(self, dca_levels: int = 5, dca_step_pct: float = 2.0,
                 multiplier: float = 1.5):
        """
        Args:
            dca_levels: максимум уровней усреднения
            dca_step_pct: шаг между уровнями DCA в %
            multiplier: множитель объёма для каждого следующего уровня
        """
        self.dca_levels = dca_levels
        self.dca_step_pct = dca_step_pct
        self.multiplier = multiplier
        self.current_level = 0
        self.entry_price = 0.0

    def analyze(self, df: pd.DataFrame, symbol: str) -> Signal:
        if len(df) < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason="Недостаточно данных")

        df = df.copy()

        # Индикаторы
        df["rsi"] = ta.momentum.rsi(df["close"], window=14)
        df["ema20"] = ta.trend.ema_indicator(df["close"], window=20)
        df["ema50"] = ta.trend.ema_indicator(df["close"], window=50)

        # MACD для определения момента
        macd = ta.trend.MACD(df["close"])
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["macd_hist"] = macd.macd_diff()

        # Уровни поддержки через пивоты
        df["support"] = df["low"].rolling(window=20).min()

        last = df.iloc[-1]
        prev = df.iloc[-2]
        current_price = last["close"]

        indicators = {
            "price": round(current_price, 2),
            "rsi": round(last["rsi"], 1),
            "macd_hist": round(last["macd_hist"], 4),
            "ema20": round(last["ema20"], 2),
            "support": round(last["support"], 2),
            "dca_level": self.current_level,
        }

        # Условия для первого входа
        if self.current_level == 0:
            # Покупаем когда: RSI < 40, MACD разворачивается, цена у поддержки
            rsi_ok = last["rsi"] < 40
            macd_turning = prev["macd_hist"] < 0 and last["macd_hist"] > prev["macd_hist"]
            near_support = current_price < last["support"] * 1.02

            if rsi_ok and (macd_turning or near_support):
                self.entry_price = current_price
                self.current_level = 1
                strength = 0.5 + (0.3 if near_support else 0) + (0.2 if macd_turning else 0)
                return Signal(
                    type=SignalType.BUY, strength=min(1.0, strength), price=current_price,
                    symbol=symbol, strategy=self.name,
                    reason=f"DCA вход #1: RSI={last['rsi']:.0f}"
                           + (", у поддержки" if near_support else "")
                           + (", MACD разворот" if macd_turning else ""),
                    indicators=indicators,
                    custom_sl_pct=self.dca_step_pct * (self.dca_levels + 1),  # широкий SL
                    custom_tp_pct=self.dca_step_pct * 2,
                )

        # Условия для дополнительных входов (усреднение)
        elif self.current_level < self.dca_levels and self.entry_price > 0:
            drop_from_entry = (self.entry_price - current_price) / self.entry_price * 100
            next_level_drop = self.dca_step_pct * self.current_level

            if drop_from_entry >= next_level_drop:
                # Дополнительный фильтр: RSI должен быть < 45
                if last["rsi"] < 45:
                    self.current_level += 1
                    strength = min(1.0, 0.3 + self.current_level * 0.15)
                    return Signal(
                        type=SignalType.BUY, strength=strength, price=current_price,
                        symbol=symbol, strategy=self.name,
                        reason=f"DCA усреднение #{self.current_level}: просадка {drop_from_entry:.1f}%",
                        indicators=indicators,
                    )

        # Условие для тейк-профита: цена вернулась выше средней входа
        if self.current_level > 0 and self.entry_price > 0:
            gain = (current_price - self.entry_price) / self.entry_price * 100
            if gain >= self.dca_step_pct * 1.5:
                if last["rsi"] > 55 and last["macd_hist"] < prev["macd_hist"]:
                    self.current_level = 0
                    self.entry_price = 0
                    return Signal(
                        type=SignalType.CLOSE_LONG, strength=0.7, price=current_price,
                        symbol=symbol, strategy=self.name,
                        reason=f"DCA тейк-профит: +{gain:.1f}%, RSI разворачивается",
                        indicators=indicators,
                    )

        return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                      reason=f"DCA уровень {self.current_level}/{self.dca_levels}",
                      indicators=indicators)

    def precompute(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["rsi"] = ta.momentum.rsi(df["close"], window=14)
        df["ema20"] = ta.trend.ema_indicator(df["close"], window=20)
        df["ema50"] = ta.trend.ema_indicator(df["close"], window=50)
        macd = ta.trend.MACD(df["close"])
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["macd_hist"] = macd.macd_diff()
        df["support"] = df["low"].rolling(window=20).min()
        return df

    def analyze_at(self, df: pd.DataFrame, idx: int, symbol: str) -> Signal:
        if idx + 1 < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason="Недостаточно данных")

        last = df.iloc[idx]
        prev = df.iloc[idx - 1]
        current_price = last["close"]

        indicators = {
            "price": round(current_price, 2),
            "rsi": round(last["rsi"], 1),
            "macd_hist": round(last["macd_hist"], 4),
            "ema20": round(last["ema20"], 2),
            "support": round(last["support"], 2),
            "dca_level": self.current_level,
        }

        # Условия для первого входа
        if self.current_level == 0:
            rsi_ok = last["rsi"] < 40
            macd_turning = prev["macd_hist"] < 0 and last["macd_hist"] > prev["macd_hist"]
            near_support = current_price < last["support"] * 1.02

            if rsi_ok and (macd_turning or near_support):
                self.entry_price = current_price
                self.current_level = 1
                strength = 0.5 + (0.3 if near_support else 0) + (0.2 if macd_turning else 0)
                return Signal(
                    type=SignalType.BUY, strength=min(1.0, strength), price=current_price,
                    symbol=symbol, strategy=self.name,
                    reason=f"DCA вход #1: RSI={last['rsi']:.0f}"
                           + (", у поддержки" if near_support else "")
                           + (", MACD разворот" if macd_turning else ""),
                    indicators=indicators,
                    custom_sl_pct=self.dca_step_pct * (self.dca_levels + 1),
                    custom_tp_pct=self.dca_step_pct * 2,
                )

        # Условия для дополнительных входов (усреднение)
        elif self.current_level < self.dca_levels and self.entry_price > 0:
            drop_from_entry = (self.entry_price - current_price) / self.entry_price * 100
            next_level_drop = self.dca_step_pct * self.current_level

            if drop_from_entry >= next_level_drop:
                if last["rsi"] < 45:
                    self.current_level += 1
                    strength = min(1.0, 0.3 + self.current_level * 0.15)
                    return Signal(
                        type=SignalType.BUY, strength=strength, price=current_price,
                        symbol=symbol, strategy=self.name,
                        reason=f"DCA усреднение #{self.current_level}: просадка {drop_from_entry:.1f}%",
                        indicators=indicators,
                    )

        # Условие для тейк-профита
        if self.current_level > 0 and self.entry_price > 0:
            gain = (current_price - self.entry_price) / self.entry_price * 100
            if gain >= self.dca_step_pct * 1.5:
                if last["rsi"] > 55 and last["macd_hist"] < prev["macd_hist"]:
                    self.current_level = 0
                    self.entry_price = 0
                    return Signal(
                        type=SignalType.CLOSE_LONG, strength=0.7, price=current_price,
                        symbol=symbol, strategy=self.name,
                        reason=f"DCA тейк-профит: +{gain:.1f}%, RSI разворачивается",
                        indicators=indicators,
                    )

        return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                      reason=f"DCA уровень {self.current_level}/{self.dca_levels}",
                      indicators=indicators)

    def reset(self) -> None:
        """Сброс состояния."""
        self.current_level = 0
        self.entry_price = 0.0
