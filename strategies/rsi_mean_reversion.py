"""
Стратегия RSI Mean Reversion — контртрендовая.

Покупает при перепроданности (RSI < 30) и продаёт при перекупленности (RSI > 70).
Использует Bollinger Bands для подтверждения и дивергенции RSI для усиления сигнала.

Подходит для: боковых рынков, ренджей.
Риск: Консервативный.
"""

import ta
import pandas as pd
from strategies.base import BaseStrategy, Signal, SignalType


class RsiMeanReversionStrategy(BaseStrategy):
    name = "rsi_mean_reversion"
    description = "RSI + Bollinger Bands mean reversion"
    timeframe = "1h"
    min_candles = 50
    risk_category = "conservative"

    def __init__(self, rsi_period: int = 14, rsi_oversold: float = 30,
                 rsi_overbought: float = 70, bb_period: int = 20, bb_std: float = 2.0):
        self.rsi_period = rsi_period
        self.rsi_oversold = rsi_oversold
        self.rsi_overbought = rsi_overbought
        self.bb_period = bb_period
        self.bb_std = bb_std

    def analyze(self, df: pd.DataFrame, symbol: str) -> Signal:
        if len(df) < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason="Недостаточно данных")

        df = df.copy()

        df["rsi"] = ta.momentum.rsi(df["close"], window=self.rsi_period)

        bb = ta.volatility.BollingerBands(df["close"], window=self.bb_period, window_dev=self.bb_std)
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_lower"] = bb.bollinger_lband()
        df["bb_mid"] = bb.bollinger_mavg()
        df["bb_width"] = df.apply(
            lambda r: (r["bb_upper"] - r["bb_lower"]) / r["bb_mid"] if r["bb_mid"] != 0 else 0, axis=1
        )

        df["stoch_rsi"] = ta.momentum.stochrsi(df["close"], window=self.rsi_period)

        last = df.iloc[-1]
        prev = df.iloc[-2]

        rsi = self.safe_val(last["rsi"], 50)
        close = self.safe_val(last["close"], 1.0)
        bb_upper = self.safe_val(last["bb_upper"], close)
        bb_lower = self.safe_val(last["bb_lower"], close)
        bb_mid = self.safe_val(last["bb_mid"], close)

        if rsi == 50 and self.safe_val(prev["rsi"], 50) == 50:
            return self._hold_signal(symbol, close, {"reason": "RSI не готов"})

        indicators = {
            "rsi": round(rsi, 1),
            "stoch_rsi": round(self.safe_val(last["stoch_rsi"]), 2),
            "bb_upper": round(bb_upper, 2),
            "bb_lower": round(bb_lower, 2),
            "bb_width": round(self.safe_val(last["bb_width"]), 4),
            "price": round(close, 2),
        }

        recent_close = df["close"].iloc[-5:].dropna()
        recent_rsi = df["rsi"].iloc[-5:].dropna()

        bullish_div = (
            len(recent_close) >= 3 and len(recent_rsi) >= 3
            and close < recent_close.min() * 1.001
            and rsi > recent_rsi.min()
        )

        bearish_div = (
            len(recent_close) >= 3 and len(recent_rsi) >= 3
            and close > recent_close.max() * 0.999
            and rsi < recent_rsi.max()
        )

        if rsi < self.rsi_oversold and close <= bb_lower * 1.005:
            strength = self.safe_div(self.rsi_oversold - rsi, self.rsi_oversold)
            if bullish_div:
                strength = min(1.0, strength + 0.3)
            return Signal(
                type=SignalType.BUY, strength=strength, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason=f"RSI={last['rsi']:.0f} перепродан, цена у нижней BB"
                       + (", бычья дивергенция" if bullish_div else ""),
                indicators=indicators,
                custom_sl_pct=1.5,
                custom_tp_pct=3.0,
            )

        if rsi > self.rsi_overbought and close >= bb_upper * 0.995:
            strength = self.safe_div(rsi - self.rsi_overbought, 100 - self.rsi_overbought)
            if bearish_div:
                strength = min(1.0, strength + 0.3)
            return Signal(
                type=SignalType.SELL, strength=strength, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason=f"RSI={last['rsi']:.0f} перекуплен, цена у верхней BB"
                       + (", медвежья дивергенция" if bearish_div else ""),
                indicators=indicators,
                custom_sl_pct=1.5,
                custom_tp_pct=3.0,
            )

        if prev["rsi"] < 45 and last["rsi"] > 50 and last["close"] > last["bb_mid"]:
            return Signal(
                type=SignalType.CLOSE_SHORT, strength=0.5, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason="RSI вернулся к 50, закрытие шорта",
                indicators=indicators,
            )

        if prev["rsi"] > 55 and last["rsi"] < 50 and last["close"] < last["bb_mid"]:
            return Signal(
                type=SignalType.CLOSE_LONG, strength=0.5, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason="RSI вернулся к 50, закрытие лонга",
                indicators=indicators,
            )

        return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                      reason=f"RSI={last['rsi']:.0f} — нейтральная зона", indicators=indicators)
