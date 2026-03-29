"""
Momentum Breakout — вход при пробое N-периодного максимума/минимума.

Донченский канал (Donchian Channel) + MACD + Volume.
Классическая трендовая система: покупаем при пробое верхней границы канала,
продаём при пробое нижней. Работает на сильных трендах.

Параметры оптимизированы для 4h BTC/USDT.
"""

import ta
import pandas as pd
from strategies.base import BaseStrategy, Signal, SignalType


class MomentumBreakoutStrategy(BaseStrategy):
    name = "momentum_breakout"
    description = "Пробой Донченского канала + MACD + объём"
    timeframe = "4h"
    min_candles = 100
    risk_category = "moderate"

    def __init__(self, channel_period: int = 40, atr_period: int = 14,
                 atr_sl_mult: float = 2.0, rr_ratio: float = 2.5,
                 volume_mult: float = 1.0):
        self.channel_period = channel_period
        self.atr_period = atr_period
        self.atr_sl_mult = atr_sl_mult
        self.rr_ratio = rr_ratio
        self.volume_mult = volume_mult

    def precompute(self, df: pd.DataFrame) -> pd.DataFrame:
        df = df.copy()
        # Донченский канал
        df["dc_upper"] = df["high"].rolling(window=self.channel_period).max()
        df["dc_lower"] = df["low"].rolling(window=self.channel_period).min()
        df["dc_mid"] = (df["dc_upper"] + df["dc_lower"]) / 2

        # MACD
        macd = ta.trend.MACD(df["close"])
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["macd_hist"] = macd.macd_diff()

        # ATR для SL
        df["atr"] = ta.volatility.average_true_range(
            df["high"], df["low"], df["close"], window=self.atr_period
        )
        df["atr_pct"] = df["atr"] / df["close"] * 100

        # Volume
        df["vol_sma"] = df["volume"].rolling(window=20).mean()

        # EMA 200 для фильтра тренда
        df["ema200"] = ta.trend.ema_indicator(df["close"], window=200)

        return df

    def analyze_at(self, df: pd.DataFrame, idx: int, symbol: str) -> Signal:
        if idx < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason="Недостаточно данных")

        last = df.iloc[idx]
        prev = df.iloc[idx - 1]

        indicators = {
            "price": round(last["close"], 2),
            "dc_upper": round(last["dc_upper"], 2),
            "dc_lower": round(last["dc_lower"], 2),
            "macd_hist": round(last["macd_hist"], 4),
            "atr_pct": round(last["atr_pct"], 2),
        }

        volume_ok = last["volume"] > last["vol_sma"] * self.volume_mult

        # === LONG: пробой верхней границы канала ===
        # Цена закрылась выше предыдущего максимума канала + MACD бычий
        breakout_up = last["close"] > prev["dc_upper"] and prev["close"] <= prev["dc_upper"]
        macd_bull = last["macd_hist"] > 0
        above_ema200 = last["close"] > last["ema200"]

        if breakout_up and macd_bull and volume_ok and above_ema200:
            atr_sl = last["atr_pct"] * self.atr_sl_mult
            sl_pct = max(2.0, min(atr_sl, 10.0))
            tp_pct = sl_pct * self.rr_ratio

            return Signal(
                type=SignalType.BUY,
                strength=0.8,
                price=last["close"],
                symbol=symbol,
                strategy=self.name,
                reason=f"Пробой верхн. канала {prev['dc_upper']:.0f}, MACD+, Vol+",
                indicators=indicators,
                custom_sl_pct=sl_pct,
                custom_tp_pct=tp_pct,
            )

        # === SHORT: пробой нижней границы канала ===
        breakout_down = last["close"] < prev["dc_lower"] and prev["close"] >= prev["dc_lower"]
        macd_bear = last["macd_hist"] < 0
        below_ema200 = last["close"] < last["ema200"]

        if breakout_down and macd_bear and volume_ok and below_ema200:
            atr_sl = last["atr_pct"] * self.atr_sl_mult
            sl_pct = max(2.0, min(atr_sl, 10.0))
            tp_pct = sl_pct * self.rr_ratio

            return Signal(
                type=SignalType.SELL,
                strength=0.8,
                price=last["close"],
                symbol=symbol,
                strategy=self.name,
                reason=f"Пробой нижн. канала {prev['dc_lower']:.0f}, MACD-, Vol+",
                indicators=indicators,
                custom_sl_pct=sl_pct,
                custom_tp_pct=tp_pct,
            )

        # === Закрытие: цена вернулась к середине канала ===
        if last["close"] < last["dc_mid"] and prev["close"] >= prev["dc_mid"]:
            return Signal(
                type=SignalType.CLOSE_LONG, strength=0.5,
                price=last["close"], symbol=symbol, strategy=self.name,
                reason="Цена вернулась к середине канала",
                indicators=indicators,
            )

        if last["close"] > last["dc_mid"] and prev["close"] <= prev["dc_mid"]:
            return Signal(
                type=SignalType.CLOSE_SHORT, strength=0.5,
                price=last["close"], symbol=symbol, strategy=self.name,
                reason="Цена вернулась к середине канала",
                indicators=indicators,
            )

        return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                      reason=f"DC: {last['dc_lower']:.0f}-{last['dc_upper']:.0f}",
                      indicators=indicators)

    def analyze(self, df: pd.DataFrame, symbol: str) -> Signal:
        if len(df) < self.min_candles:
            return Signal(type=SignalType.HOLD, symbol=symbol, strategy=self.name,
                          reason="Недостаточно данных")
        df = self.precompute(df)
        return self.analyze_at(df, len(df) - 1, symbol)
