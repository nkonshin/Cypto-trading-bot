"""
Стратегия EMA Crossover — трендовая стратегия.

Использует пересечение быстрой и медленной EMA для определения тренда.
Дополнительно фильтрует по EMA 200 (глобальный тренд) и объёму.

Подходит для: трендовых рынков, средний таймфрейм (1h-4h).
Риск: Умеренный.
"""

import ta
import pandas as pd
from strategies.base import BaseStrategy, Signal, SignalType


class EmaCrossoverStrategy(BaseStrategy):
    name = "ema_crossover"
    description = "Пересечение EMA с фильтром тренда"
    timeframe = "1h"
    min_candles = 210
    risk_category = "moderate"

    def __init__(self, fast_period: int = 9, slow_period: int = 21, trend_period: int = 200):
        self.fast_period = fast_period
        self.slow_period = slow_period
        self.trend_period = trend_period

    def analyze(self, df: pd.DataFrame, symbol: str) -> Signal:
        if len(df) < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason="Недостаточно данных")

        # Рассчитываем EMA
        df = df.copy()
        df["ema_fast"] = ta.trend.ema_indicator(df["close"], window=self.fast_period)
        df["ema_slow"] = ta.trend.ema_indicator(df["close"], window=self.slow_period)
        df["ema_trend"] = ta.trend.ema_indicator(df["close"], window=self.trend_period)

        # Объём — SMA для сравнения
        df["vol_sma"] = df["volume"].rolling(window=20).mean()

        last = df.iloc[-1]
        prev = df.iloc[-2]

        indicators = {
            "ema_fast": round(last["ema_fast"], 2),
            "ema_slow": round(last["ema_slow"], 2),
            "ema_trend": round(last["ema_trend"], 2),
            "price": round(last["close"], 2),
        }

        # Золотой крест: быстрая EMA пересекает медленную снизу вверх
        golden_cross = prev["ema_fast"] <= prev["ema_slow"] and last["ema_fast"] > last["ema_slow"]
        # Мёртвый крест: быстрая EMA пересекает медленную сверху вниз
        death_cross = prev["ema_fast"] >= prev["ema_slow"] and last["ema_fast"] < last["ema_slow"]

        # Фильтр глобального тренда
        uptrend = last["close"] > last["ema_trend"]
        downtrend = last["close"] < last["ema_trend"]

        # Фильтр объёма (объём выше среднего)
        volume_ok = last["volume"] > last["vol_sma"] * 0.8

        if golden_cross and uptrend and volume_ok:
            strength = min(1.0, (last["ema_fast"] - last["ema_slow"]) / last["close"] * 100)
            return Signal(
                type=SignalType.BUY, strength=abs(strength), price=last["close"],
                symbol=symbol, strategy=self.name,
                reason=f"Золотой крест EMA{self.fast_period}/{self.slow_period}, цена выше EMA{self.trend_period}",
                indicators=indicators,
            )

        if death_cross and downtrend and volume_ok:
            strength = min(1.0, (last["ema_slow"] - last["ema_fast"]) / last["close"] * 100)
            return Signal(
                type=SignalType.SELL, strength=abs(strength), price=last["close"],
                symbol=symbol, strategy=self.name,
                reason=f"Мёртвый крест EMA{self.fast_period}/{self.slow_period}, цена ниже EMA{self.trend_period}",
                indicators=indicators,
            )

        # Сигнал на закрытие позиции при обратном пересечении
        if death_cross and not downtrend:
            return Signal(
                type=SignalType.CLOSE_LONG, strength=0.5, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason="Мёртвый крест — закрытие лонга",
                indicators=indicators,
            )

        if golden_cross and not uptrend:
            return Signal(
                type=SignalType.CLOSE_SHORT, strength=0.5, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason="Золотой крест — закрытие шорта",
                indicators=indicators,
            )

        return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                      reason="Нет сигнала", indicators=indicators)

    def precompute(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["ema_fast"] = ta.trend.ema_indicator(df["close"], window=self.fast_period)
        df["ema_slow"] = ta.trend.ema_indicator(df["close"], window=self.slow_period)
        df["ema_trend"] = ta.trend.ema_indicator(df["close"], window=self.trend_period)
        df["vol_sma"] = df["volume"].rolling(window=20).mean()
        return df

    def analyze_at(self, df: pd.DataFrame, idx: int, symbol: str) -> Signal:
        if idx + 1 < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason="Недостаточно данных")

        last = df.iloc[idx]
        prev = df.iloc[idx - 1]

        indicators = {
            "ema_fast": round(last["ema_fast"], 2),
            "ema_slow": round(last["ema_slow"], 2),
            "ema_trend": round(last["ema_trend"], 2),
            "price": round(last["close"], 2),
        }

        golden_cross = prev["ema_fast"] <= prev["ema_slow"] and last["ema_fast"] > last["ema_slow"]
        death_cross = prev["ema_fast"] >= prev["ema_slow"] and last["ema_fast"] < last["ema_slow"]

        uptrend = last["close"] > last["ema_trend"]
        downtrend = last["close"] < last["ema_trend"]

        volume_ok = last["volume"] > last["vol_sma"] * 0.8

        if golden_cross and uptrend and volume_ok:
            strength = min(1.0, (last["ema_fast"] - last["ema_slow"]) / last["close"] * 100)
            return Signal(
                type=SignalType.BUY, strength=abs(strength), price=last["close"],
                symbol=symbol, strategy=self.name,
                reason=f"Золотой крест EMA{self.fast_period}/{self.slow_period}, цена выше EMA{self.trend_period}",
                indicators=indicators,
            )

        if death_cross and downtrend and volume_ok:
            strength = min(1.0, (last["ema_slow"] - last["ema_fast"]) / last["close"] * 100)
            return Signal(
                type=SignalType.SELL, strength=abs(strength), price=last["close"],
                symbol=symbol, strategy=self.name,
                reason=f"Мёртвый крест EMA{self.fast_period}/{self.slow_period}, цена ниже EMA{self.trend_period}",
                indicators=indicators,
            )

        if death_cross and not downtrend:
            return Signal(
                type=SignalType.CLOSE_LONG, strength=0.5, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason="Мёртвый крест — закрытие лонга",
                indicators=indicators,
            )

        if golden_cross and not uptrend:
            return Signal(
                type=SignalType.CLOSE_SHORT, strength=0.5, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason="Золотой крест — закрытие шорта",
                indicators=indicators,
            )

        return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                      reason="Нет сигнала", indicators=indicators)
