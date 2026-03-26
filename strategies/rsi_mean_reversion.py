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

        # RSI
        df["rsi"] = ta.momentum.rsi(df["close"], window=self.rsi_period)

        # Bollinger Bands
        bb = ta.volatility.BollingerBands(df["close"], window=self.bb_period, window_dev=self.bb_std)
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_lower"] = bb.bollinger_lband()
        df["bb_mid"] = bb.bollinger_mavg()
        df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]

        # Stochastic RSI для дополнительного подтверждения
        df["stoch_rsi"] = ta.momentum.stochrsi(df["close"], window=self.rsi_period)

        last = df.iloc[-1]
        prev = df.iloc[-2]

        indicators = {
            "rsi": round(last["rsi"], 1),
            "stoch_rsi": round(last["stoch_rsi"], 2),
            "bb_upper": round(last["bb_upper"], 2),
            "bb_lower": round(last["bb_lower"], 2),
            "bb_width": round(last["bb_width"], 4),
            "price": round(last["close"], 2),
        }

        # RSI дивергенция (бычья): цена делает новый лоу, RSI — нет
        bullish_div = (
            last["close"] < df["close"].iloc[-5:].min() * 1.001
            and last["rsi"] > df["rsi"].iloc[-5:].min()
        )

        # RSI дивергенция (медвежья)
        bearish_div = (
            last["close"] > df["close"].iloc[-5:].max() * 0.999
            and last["rsi"] < df["rsi"].iloc[-5:].max()
        )

        # BUY: RSI перепродан + цена у нижней BB
        if last["rsi"] < self.rsi_oversold and last["close"] <= last["bb_lower"] * 1.005:
            strength = (self.rsi_oversold - last["rsi"]) / self.rsi_oversold
            if bullish_div:
                strength = min(1.0, strength + 0.3)
            return Signal(
                type=SignalType.BUY, strength=strength, price=last["close"],
                symbol=symbol, strategy=self.name,
                reason=f"RSI={last['rsi']:.0f} перепродан, цена у нижней BB"
                       + (", бычья дивергенция" if bullish_div else ""),
                indicators=indicators,
                custom_sl_pct=1.5,  # тесный стоп для mean reversion
                custom_tp_pct=3.0,
            )

        # SELL: RSI перекуплен + цена у верхней BB
        if last["rsi"] > self.rsi_overbought and last["close"] >= last["bb_upper"] * 0.995:
            strength = (last["rsi"] - self.rsi_overbought) / (100 - self.rsi_overbought)
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

        # Close signals: RSI возвращается к нейтральной зоне
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

    def precompute(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        df["rsi"] = ta.momentum.rsi(df["close"], window=self.rsi_period)
        bb = ta.volatility.BollingerBands(df["close"], window=self.bb_period, window_dev=self.bb_std)
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_lower"] = bb.bollinger_lband()
        df["bb_mid"] = bb.bollinger_mavg()
        df["bb_width"] = (df["bb_upper"] - df["bb_lower"]) / df["bb_mid"]
        df["stoch_rsi"] = ta.momentum.stochrsi(df["close"], window=self.rsi_period)
        return df

    def analyze_at(self, df: pd.DataFrame, idx: int, symbol: str) -> Signal:
        if idx + 1 < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason="Недостаточно данных")

        last = df.iloc[idx]
        prev = df.iloc[idx - 1]

        indicators = {
            "rsi": round(last["rsi"], 1),
            "stoch_rsi": round(last["stoch_rsi"], 2),
            "bb_upper": round(last["bb_upper"], 2),
            "bb_lower": round(last["bb_lower"], 2),
            "bb_width": round(last["bb_width"], 4),
            "price": round(last["close"], 2),
        }

        # RSI дивергенция (бычья)
        s = max(0, idx - 4)
        bullish_div = (
            last["close"] < df["close"].iloc[s:idx + 1].min() * 1.001
            and last["rsi"] > df["rsi"].iloc[s:idx + 1].min()
        )

        # RSI дивергенция (медвежья)
        bearish_div = (
            last["close"] > df["close"].iloc[s:idx + 1].max() * 0.999
            and last["rsi"] < df["rsi"].iloc[s:idx + 1].max()
        )

        # BUY
        if last["rsi"] < self.rsi_oversold and last["close"] <= last["bb_lower"] * 1.005:
            strength = (self.rsi_oversold - last["rsi"]) / self.rsi_oversold
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

        # SELL
        if last["rsi"] > self.rsi_overbought and last["close"] >= last["bb_upper"] * 0.995:
            strength = (last["rsi"] - self.rsi_overbought) / (100 - self.rsi_overbought)
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

        # Close signals
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
