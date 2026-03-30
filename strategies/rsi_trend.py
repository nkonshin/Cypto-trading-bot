"""
RSI Trend — вход по RSI с фильтром тренда.

Только лонги в бычьем тренде (EMA50 > EMA200), только шорты в медвежьем.
RSI < порога → покупка, RSI > порога → закрытие (не шорт!).
Широкий SL (8-12%) чтобы дать сигналу отработать.

Основано на классическом подходе: RSI + тренд-фильтр.
"""

import ta
import pandas as pd
from strategies.base import BaseStrategy, Signal, SignalType


class RsiTrendStrategy(BaseStrategy):
    name = "rsi_trend"
    description = "RSI + тренд-фильтр (только по тренду, широкий SL)"
    timeframe = "4h"
    min_candles = 210
    risk_category = "moderate"

    def __init__(self, rsi_period: int = 14,
                 rsi_buy: float = 30, rsi_sell: float = 70,
                 rsi_close_long: float = 65, rsi_close_short: float = 35,
                 ema_fast: int = 50, ema_slow: int = 200,
                 sl_pct: float = 10.0, tp_pct: float = 20.0):
        self.rsi_period = rsi_period
        self.rsi_buy = rsi_buy
        self.rsi_sell = rsi_sell
        self.rsi_close_long = rsi_close_long
        self.rsi_close_short = rsi_close_short
        self.ema_fast = ema_fast
        self.ema_slow = ema_slow
        self.sl_pct = sl_pct
        self.tp_pct = tp_pct

    def precompute(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["rsi"] = ta.momentum.rsi(df["close"], window=self.rsi_period)
        df["ema_fast"] = ta.trend.ema_indicator(df["close"], window=self.ema_fast)
        df["ema_slow"] = ta.trend.ema_indicator(df["close"], window=self.ema_slow)
        df["vol_sma"] = df["volume"].rolling(window=20).mean()
        return df

    def analyze_at(self, df: pd.DataFrame, idx: int, symbol: str) -> Signal:
        if idx < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason="Недостаточно данных")

        last = df.iloc[idx]
        prev = df.iloc[idx - 1]

        rsi = last["rsi"]
        prev_rsi = prev["rsi"]
        uptrend = last["ema_fast"] > last["ema_slow"]
        downtrend = last["ema_fast"] < last["ema_slow"]

        indicators = {
            "rsi": round(rsi, 1),
            "ema_fast": round(last["ema_fast"], 2),
            "ema_slow": round(last["ema_slow"], 2),
            "trend": "UP" if uptrend else "DOWN" if downtrend else "FLAT",
        }

        # === ЛОНГ: RSI < порога И бычий тренд ===
        if rsi < self.rsi_buy and uptrend:
            return Signal(
                type=SignalType.BUY, strength=0.8, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason=f"RSI={rsi:.0f} < {self.rsi_buy}, тренд UP",
                indicators=indicators,
                custom_sl_pct=self.sl_pct,
                custom_tp_pct=self.tp_pct,
            )

        # === ШОРТ: RSI > порога И медвежий тренд ===
        if rsi > self.rsi_sell and downtrend:
            return Signal(
                type=SignalType.SELL, strength=0.8, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason=f"RSI={rsi:.0f} > {self.rsi_sell}, тренд DOWN",
                indicators=indicators,
                custom_sl_pct=self.sl_pct,
                custom_tp_pct=self.tp_pct,
            )

        # === Закрытие лонга: RSI вырос выше порога ===
        if prev_rsi < self.rsi_close_long and rsi >= self.rsi_close_long:
            return Signal(
                type=SignalType.CLOSE_LONG, strength=0.6, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason=f"RSI={rsi:.0f} достиг {self.rsi_close_long}, фиксация",
                indicators=indicators,
            )

        # === Закрытие шорта: RSI упал ниже порога ===
        if prev_rsi > self.rsi_close_short and rsi <= self.rsi_close_short:
            return Signal(
                type=SignalType.CLOSE_SHORT, strength=0.6, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason=f"RSI={rsi:.0f} достиг {self.rsi_close_short}, фиксация",
                indicators=indicators,
            )

        # === Закрытие при смене тренда ===
        prev_uptrend = prev["ema_fast"] > prev["ema_slow"]
        prev_downtrend = prev["ema_fast"] < prev["ema_slow"]

        if prev_uptrend and downtrend:
            return Signal(
                type=SignalType.CLOSE_LONG, strength=0.7, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason="Тренд сменился на DOWN, закрытие лонга",
                indicators=indicators,
            )

        if prev_downtrend and uptrend:
            return Signal(
                type=SignalType.CLOSE_SHORT, strength=0.7, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason="Тренд сменился на UP, закрытие шорта",
                indicators=indicators,
            )

        return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                      reason=f"RSI={rsi:.0f}, тренд={'UP' if uptrend else 'DOWN' if downtrend else 'FLAT'}",
                      indicators=indicators)

    def analyze(self, df: pd.DataFrame, symbol: str) -> Signal:
        if len(df) < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason="Недостаточно данных")
        df = self.precompute(df)
        return self.analyze_at(df, len(df) - 1, symbol)
